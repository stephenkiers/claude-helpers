#!/usr/bin/env python3
"""
In-process single-rule mutants for the deny rules of scripts/flow-bash-guard.py (#404).

Each deny-rule table entry is removed in turn (one at a time, restored in a finally block) and
the single corpus command that only that rule catches must flip from blocked to allowed. This
proves the rule is load-bearing, not merely present. There is no source rewriting and no
subprocess per mutant: the module is loaded once with importlib and each mutant patches the
module attribute, which the guard reads at call time.

Named tables covered (D1, no generic registry):
- WRAPPER_FORMS
- PUBLISHING_VERBS
- TRANSPARENT_PREFIXES (the #403 command-prefix normaliser; one entry per Q2 rule)

Mutation policy: every entry in these three tables needs a single-rule corpus command here, and
a coverage test fails when one is missing. A future deny-rule table (for example #359's per-agent
table) must add its own corpus entries and mutants when it lands.

Run with: python3 tests/test_flow_bash_guard_mutants.py
"""

import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


BASH_GUARD_PATH = REPO_ROOT / "scripts" / "flow-bash-guard.py"

WRAPPER_TABLE = "WRAPPER_FORMS"
VERB_TABLE = "PUBLISHING_VERBS"
PREFIX_TABLE = "TRANSPARENT_PREFIXES"

# One single-rule corpus command per table entry. Each command must be blocked by its own entry
# and by no other entry, so removing that entry alone must make it allowed.
CORPUS = {
    # WRAPPER_FORMS (keyed by the wrapper string)
    (WRAPPER_TABLE, "sh -c"): 'sh -c "ls"',
    (WRAPPER_TABLE, "bash -c"): 'bash -c "ls"',
    (WRAPPER_TABLE, "zsh -c"): 'zsh -c "ls"',
    (WRAPPER_TABLE, "eval"): "eval ls",
    (WRAPPER_TABLE, "xargs git"): "xargs git push",
    (WRAPPER_TABLE, "xargs gh"): "xargs gh pr create",
    (WRAPPER_TABLE, "env git"): "env git push",
    (WRAPPER_TABLE, "env gh"): "env gh pr create",
    (WRAPPER_TABLE, "git -c"): "git -c x=y status",
    (WRAPPER_TABLE, "git --git-dir"): "git --git-dir=/x status",
    (WRAPPER_TABLE, "git --exec-path"): "git --exec-path=/x status",
    (WRAPPER_TABLE, "gh api -X non-GET"): "gh api -X POST repos/o/r/issues",

    # PUBLISHING_VERBS (keyed by the verb tuple)
    (VERB_TABLE, ("git", "push")): "git push origin x",
    (VERB_TABLE, ("git", "remote", "add")): "git remote add origin x",
    (VERB_TABLE, ("git", "remote", "set-url")): "git remote set-url origin x",
    (VERB_TABLE, ("gh", "pr", "create")): "gh pr create",
    (VERB_TABLE, ("gh", "pr", "merge")): "gh pr merge 1",
    (VERB_TABLE, ("gh", "pr", "edit")): "gh pr edit 1",
    (VERB_TABLE, ("gh", "pr", "comment")): "gh pr comment 1",
    (VERB_TABLE, ("gh", "pr", "close")): "gh pr close 1",
    (VERB_TABLE, ("gh", "pr", "review")): "gh pr review 1",
    (VERB_TABLE, ("gh", "issue", "create")): "gh issue create",
    (VERB_TABLE, ("gh", "issue", "edit")): "gh issue edit 1",
    (VERB_TABLE, ("gh", "issue", "comment")): "gh issue comment 1",
    (VERB_TABLE, ("gh", "issue", "close")): "gh issue close 1",
    (VERB_TABLE, ("gh", "release", "create")): "gh release create v1",
    (VERB_TABLE, ("gh", "repo", "create")): "gh repo create x",
    (VERB_TABLE, ("gh", "repo", "delete")): "gh repo delete x",
    (VERB_TABLE, ("gh", "gist", "create")): "gh gist create f",
    (VERB_TABLE, ("gh", "workflow", "run")): "gh workflow run ci",

    # TRANSPARENT_PREFIXES (keyed by the (kind, word) pair). Each entry is one #403 Q2 rule:
    # NAME=value, the four bare words, timeout <duration>, nice -n <N>, and zsh =cmd.
    (PREFIX_TABLE, ("assignment", None)): "GIT_TERMINAL_PROMPT=0 git push",
    (PREFIX_TABLE, ("bare", "command")): "command git push",
    (PREFIX_TABLE, ("bare", "exec")): "exec git push",
    (PREFIX_TABLE, ("bare", "nohup")): "nohup git push",
    (PREFIX_TABLE, ("bare", "time")): "time git push",
    (PREFIX_TABLE, ("one-positional", "timeout")): "timeout 5 git push",
    (PREFIX_TABLE, ("nice", "nice")): "nice -n 10 git push",
    (PREFIX_TABLE, ("zsh-equals", None)): "=git push",
}

PREFIX_NAMES = (
    ("assignment", None),
    ("bare", "command"),
    ("bare", "exec"),
    ("bare", "nohup"),
    ("bare", "time"),
    ("one-positional", "timeout"),
    ("nice", "nice"),
    ("zsh-equals", None),
)


def load_guard():
    """Load flow-bash-guard.py as a module without running main()."""
    spec = importlib.util.spec_from_file_location("flow_bash_guard_under_test", BASH_GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def replaced(mod, name, value):
    """Temporarily set mod.<name> = value; restore (or delete) in a finally block."""
    had = hasattr(mod, name)
    original = getattr(mod, name, None)
    setattr(mod, name, value)
    try:
        yield
    finally:
        if had:
            setattr(mod, name, original)
        else:
            delattr(mod, name)


def is_blocked(mod, command):
    """Ask the guard, reading mod.check_command at call time so patches take effect."""
    blocked, _reason = mod.check_command(command)
    return blocked


def live_rules(mod):
    """Every (table, entry) pair currently present in the guard's named tables."""
    rules = []
    for table in (WRAPPER_TABLE, VERB_TABLE, PREFIX_TABLE):
        for entry in getattr(mod, table, ()):
            rules.append((table, entry))
    return rules


def main():
    h = Harness("FLOW BASH GUARD MUTANT TESTS (#404)")
    mod = load_guard()

    # ========================================================================
    # SECTION 1: Pre-check and corpus coverage
    # ========================================================================
    print("[Section 1] Pre-check and corpus coverage")

    unblocked = [cmd for cmd in CORPUS.values() if not is_blocked(mod, cmd)]
    h.test_result(
        "pre-check: every deny-corpus command is blocked before any mutation",
        not unblocked,
        f"allowed on the unmodified guard: {unblocked}",
    )

    live = live_rules(mod)
    missing = [rule for rule in live if rule not in CORPUS]
    h.test_result(
        "coverage: every WRAPPER_FORMS, PUBLISHING_VERBS and TRANSPARENT_PREFIXES entry has a single-rule corpus command",
        not missing,
        f"entries without a corpus command: {missing}",
    )

    stale = [rule for rule in CORPUS if rule not in live]
    h.test_result(
        "coverage: every corpus entry names a live table entry (no stale corpus rows)",
        not stale,
        f"corpus rows with no live table entry: {stale}",
    )

    # ========================================================================
    # SECTION 2: Single-rule mutants (one per table entry)
    # ========================================================================
    print("\n[Section 2] Single-rule mutants: removing one entry allows its corpus command")

    for table, entry in live:
        label = f"mutant: {table} without {entry!r} allows its single-rule command"
        original = tuple(getattr(mod, table))
        without = tuple(e for e in original if e != entry)
        with replaced(mod, table, type(original)(without)):
            applied = (
                len(getattr(mod, table)) == len(original) - 1
                and entry not in getattr(mod, table)
            )
            command = CORPUS.get((table, entry))
            flipped = command is not None and not is_blocked(mod, command)
        h.test_result(
            label,
            applied and flipped,
            f"applied={applied} command={command!r} flipped_to_allowed={flipped}",
        )

    # ========================================================================
    # SECTION 3: Prefix normaliser dispatches on the table only
    # ========================================================================
    print("\n[Section 3] TRANSPARENT_PREFIXES drives the normaliser (no hard-coded prefix branch)")

    prefix_commands = [CORPUS[(PREFIX_TABLE, name)] for name in PREFIX_NAMES if (PREFIX_TABLE, name) in CORPUS]
    # Non-vacuity: with the live table, every prefix command must be blocked first.
    live_blocked = [cmd for cmd in prefix_commands if is_blocked(mod, cmd)]
    with replaced(mod, PREFIX_TABLE, ()):
        still_blocked = [cmd for cmd in prefix_commands if is_blocked(mod, cmd)]
    h.test_result(
        "empty TRANSPARENT_PREFIXES: every prefix corpus command is allowed (live table blocks all of them first)",
        len(prefix_commands) == len(PREFIX_NAMES) and len(live_blocked) == len(prefix_commands) and not still_blocked,
        f"live-blocked={len(live_blocked)}/{len(prefix_commands)} still blocked when empty: {still_blocked}",
    )

    helper = getattr(mod, "strip_command_prefix", None)
    h.test_result("strip_command_prefix is defined in the guard", helper is not None)
    if helper is not None:
        tokens = ["GIT_TERMINAL_PROMPT=0", "git", "push"]
        with replaced(mod, PREFIX_TABLE, ()):
            unchanged = list(helper(list(tokens)))
        h.test_result(
            "strip_command_prefix with an empty table returns the tokens unchanged",
            unchanged == tokens,
            f"got {unchanged!r}",
        )
        normalised = list(helper(list(tokens)))
        h.test_result(
            "strip_command_prefix with the live table drops the NAME=value prefix",
            normalised == ["git", "push"],
            f"got {normalised!r}",
        )

    # ========================================================================
    # SECTION 4: Full revert (allow-all check_command)
    # ========================================================================
    print("\n[Section 4] Full revert: an allow-all check_command turns the corpus red")

    def allow_all(_command):
        return False, ""

    with replaced(mod, "check_command", allow_all):
        allowed_under_revert = [cmd for cmd in CORPUS.values() if not is_blocked(mod, cmd)]
    h.test_result(
        "allow-all check_command: every corpus command is allowed (pre-check would fail)",
        len(allowed_under_revert) == len(CORPUS),
        f"allowed {len(allowed_under_revert)} of {len(CORPUS)}",
    )

    # The live guard is restored after every patch: the pre-check still holds.
    restored = [cmd for cmd in CORPUS.values() if not is_blocked(mod, cmd)]
    h.test_result(
        "after all mutants are restored, every corpus command is blocked again",
        not restored,
        f"still allowed after restore: {restored}",
    )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
