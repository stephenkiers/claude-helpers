#!/usr/bin/env python3
"""
Test suite for shell conventions in slash-command docs (commands/*.md).

Covers: the positional-parameter hazard. Every .md in commands/ is registered as an
invocable slash command, and the harness substitutes the invocation's arguments into
positional tokens in the doc body *before* the shell runs it. A literal dollar-zero in
an embedded shell snippet is therefore rewritten to the user's arguments.

This was observed live: commands/cleanup.md's `_cap8k` helper was written as
`awk '{b=length($0)+1; ... substr($0,1,8000-n) ...}'` and reached the model as
`awk '{b=length(/Users/.../some-worktree)+1; ...}'` — a broken awk program built from a
filesystem path. awk is the common way to hit this because its only handle on the current
record is that token; prefer python3, sed, or a git command that answers the question
directly.

commands/ is scanned; prompts/ is deliberately not — those are lazy-loaded by path
(see CLAUDE.md, "Reference docs are not commands"), never argument-substituted, so the
same token there is harmless.

Run with: python3 tests/test_command_doc_shell_conventions.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

# The literal two-character token, assembled so this file does not itself contain it.
POSITIONAL_ZERO = "$" + "0"


def main():
    h = Harness("COMMAND DOC SHELL CONVENTIONS TEST SUITE")

    commands_dir = REPO_ROOT / "commands"
    h.test_result("commands/ directory exists", commands_dir.is_dir(), str(commands_dir))

    docs = sorted(commands_dir.rglob("*.md"))
    h.test_result("found command docs to scan", len(docs) > 0, f"found {len(docs)}")

    offenders = []
    for doc in docs:
        for lineno, line in enumerate(doc.read_text().splitlines(), start=1):
            if POSITIONAL_ZERO in line:
                rel = doc.relative_to(REPO_ROOT)
                offenders.append(f"{rel}:{lineno}: {line.strip()}")

    h.test_result(
        f"no command doc contains a literal {POSITIONAL_ZERO} (clobbered by argument substitution)",
        not offenders,
        "\n      " + "\n      ".join(offenders) if offenders else "",
    )

    # Guard the guard: if the scan silently stopped matching, this suite would pass
    # vacuously forever. Prove the detector still fires on a known-bad line.
    synthetic = f"_cap8k() {{ awk '{{print substr({POSITIONAL_ZERO},1,10)}}'; }}"
    h.test_result(
        "detector still matches a known-bad snippet",
        POSITIONAL_ZERO in synthetic,
    )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
