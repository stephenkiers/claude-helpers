#!/usr/bin/env python3
"""
PreToolUse (Bash) hook guarding Phase 2 of prompts/haiku-test-poisoning-cleanup.md.

Inert unless ~/.claude/test-cleanup/ACTIVE exists and names the repository root of the command's
cwd. While active it denies destructive git (switch -c, rm, revert, restore, reset, clean,
checkout) unless the current branch is test-cleanup/*; a branch creation must itself name a
test-cleanup/* branch. Pushes, merges, rebases and stashes are always denied.

Threat model: a careless run, not an adversarial one. Regex tokenizing, over-blocking by design;
shell-wrapper evasions are out of scope. Exit 2 blocks; any other exit allows.
"""

import json
import os
import re
import subprocess
import sys

BRANCH_PREFIX = "test-cleanup/"
GUARDED = {"rm", "revert", "restore", "reset", "clean", "checkout", "switch"}
ALWAYS_DENIED = {"push", "merge", "rebase", "stash"}
ACTIVE = os.path.join(os.path.expanduser("~"), ".claude", "test-cleanup", "ACTIVE")


def _git(cwd, *args):
    try:
        out = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def git_invocations(command):
    """Yield the token list after `git` for each git call in a shell command line."""
    for segment in re.split(r"&&|\|\||;|\||\n", command):
        tokens = segment.replace("'", " ").replace('"', " ").split()
        for i, tok in enumerate(tokens):
            if tok == "git" or tok.endswith("/git"):
                rest = tokens[i + 1:]
                # Skip git's own options (-c k=v, -C dir, --no-pager ...) to find the subcommand.
                while rest and rest[0].startswith("-"):
                    rest = rest[2:] if rest[0] in ("-c", "-C") else rest[1:]
                if rest:
                    yield rest


def decide(command, branch):
    """Return a denial message, or None to allow."""
    for rest in git_invocations(command):
        sub, args = rest[0], rest[1:]
        if sub in ALWAYS_DENIED:
            return f"git {sub} is never allowed during a test-cleanup run."
        if sub not in GUARDED:
            continue
        creates = (sub == "switch" and any(a in ("-c", "-C", "--create") for a in args)) or (
            sub == "checkout" and any(a in ("-b", "-B") for a in args))
        if creates:
            if not any(a.startswith(BRANCH_PREFIX) for a in args):
                return f"a branch created during a test-cleanup run must be named {BRANCH_PREFIX}*."
            continue
        if sub == "switch":
            return "git switch to another branch is not allowed during a test-cleanup run."
        if not (branch or "").startswith(BRANCH_PREFIX):
            return (f"git {sub} is only allowed on a {BRANCH_PREFIX}* branch "
                    f"(current: {branch or 'unknown'}).")
    return None


def main():
    try:
        event = json.load(sys.stdin)
        command = event["tool_input"]["command"]
        cwd = event.get("cwd") or os.getcwd()
    except (ValueError, KeyError, TypeError):
        return 0  # not a Bash event we understand; fail open like the other guards
    try:
        with open(ACTIVE, encoding="utf-8") as fh:
            active_root = os.path.realpath(fh.read().strip())
    except OSError:
        return 0
    top = _git(cwd, "rev-parse", "--show-toplevel")
    if top is None or os.path.realpath(top) != active_root:
        return 0
    message = decide(command, _git(cwd, "branch", "--show-current"))
    if message:
        print(f"BLOCKED by test-cleanup-git-guard: {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
