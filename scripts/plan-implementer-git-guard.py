#!/usr/bin/env python3
"""
PreToolUse hook for the plan-implementer agent: blocks destructive git subcommands.

Threat model: a careless agent, not an adversarial one. This is a guardrail, not a sandbox —
shell-wrapper evasions (`sh -c "git reset"`, npm scripts that shell out to git, `xargs git`, etc.)
are explicitly out of scope. The tokenizer below is a regex/whitespace approximation of shell
parsing, not a real shell parser, so it is over-blocking by design: prefer a false positive
(blocking a benign command) to a false negative (letting a destructive one through). If it can't
confidently parse the command, it blocks.

Allowlist over blocklist: plan-implementer is staging-only, so every git subcommand it needs is
enumerated below; anything else — known destructive commands (reset, checkout, stash, clean) and
anything not yet seen — is blocked.
"""

import json
import os
import re
import sys

ALLOWED_SUBCOMMANDS = {
    "rev-parse",
    "add",
    "status",
    "diff",
    "log",
    "show",
    "ls-files",
    "grep",
}

def _build_block_message():
    """Build BLOCK_MESSAGE from ALLOWED_SUBCOMMANDS to avoid duplication."""
    allowed_list = ", ".join(sorted(ALLOWED_SUBCOMMANDS))
    return (
        "BLOCKED: git {subcmd} is not permitted for plan-implementer. You are staging-only: use "
        f"git {allowed_list}. Destructive git operations "
        "(reset/checkout/stash/clean) destroy the staged work the orchestrator harvests; "
        "commits/pushes belong to the orchestrator. If genuinely blocked, report `STAGED: no` with "
        "the reason."
    )

BLOCK_MESSAGE = _build_block_message()

PARSE_ERROR_MESSAGE = "BLOCKED: git guard could not parse tool input; failing closed."

# Split shell metacharacters into their own tokens so a token like `--git-dir` (which contains
# the substring "git" but is not the `git` command) never gets misparsed as one.
_METACHAR_RE = re.compile(r"([();&|`])")


def tokenize(command):
    """Split command into tokens, treating shell metacharacters as separate tokens.

    Does not do shell-aware quote parsing; a command like "git 'add'" will not match
    the `git` token after quote removal.
    """
    spaced = _METACHAR_RE.sub(r" \1 ", command)
    return spaced.split()


def find_git_subcommands(command):
    """Return the (lowercased) subcommand for every top-level `git` invocation in the command."""
    tokens = tokenize(command)
    subcommands = []
    i = 0
    n = len(tokens)
    while i < n:
        # Check if token is 'git' (case-insensitive), stripping quotes and using basename
        # to catch quoted invocations ("git", 'git') and path-qualified ones (/usr/bin/git).
        token_basename = os.path.basename(tokens[i].strip('\'"')).lower()
        if token_basename == "git":
            j = i + 1
            while j < n:
                tok = tokens[j]
                if tok in ("-C", "-c"):
                    j += 2
                    continue
                if tok.startswith("-C") and len(tok) > 2:
                    j += 1
                    continue
                if tok.startswith("-c") and len(tok) > 2:
                    j += 1
                    continue
                break
            subcommands.append(tokens[j].lower() if j < n else "<none>")
            i = j
        else:
            i += 1
    return subcommands


def main():
    try:
        payload = json.load(sys.stdin)
        command = payload["tool_input"]["command"]
        if not isinstance(command, str):
            raise ValueError("tool_input.command is not a string")

        for subcmd in find_git_subcommands(command):
            if subcmd not in ALLOWED_SUBCOMMANDS:
                print(BLOCK_MESSAGE.format(subcmd=subcmd), file=sys.stderr)
                sys.exit(2)
    except Exception:
        print(PARSE_ERROR_MESSAGE, file=sys.stderr)
        sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
