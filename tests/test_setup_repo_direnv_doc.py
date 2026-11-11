#!/usr/bin/env python3
"""
Wording and structure contract for the optional direnv step in commands/setup-repo.md (#444).

The step is shell embedded in a command doc, so these checks pin the decided behaviour a
reader or a step agent depends on: the exact consent question, the branch allowlist, the
dotenv convention line written to worktrees/.envrc, the absence of dotenv_if_exists, the
rule that direnv allow runs only on a file this run wrote, the hand-off ### direnv block,
and the ban on bare positional tokens in command docs.

Run with: python3 tests/test_setup_repo_direnv_doc.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

SETUP_REPO = REPO_ROOT / "commands" / "setup-repo.md"

QUESTION = "Use direnv for environment variables in this project?"
Q1_ALLOWLIST_REGEX = "grep -Eq '^[A-Za-z0-9._/-]+$'"
DOTENV_LINE = 'if [[ -f "<b>/.env" ]]; then dotenv "<b>/.env"; fi'
# The generated file is written with printf; <b> is the branch placeholder.
DOTENV_FORMAT = 'if [[ -f "%s/.env" ]]; then dotenv "%s/.env"; fi'
ALLOW_LINE = 'if direnv allow "$ENVRC"'
WRITTEN_GUARD = 'ENVRC_STATE" = written'
HANDOFF_HEADING = "### direnv"


def read(path):
    """Return a file's text, or '' if missing."""
    try:
        return path.read_text()
    except OSError:
        return ""


if __name__ == "__main__":
    h = Harness("SETUP-REPO DIRENV DOC TEST SUITE")
    test_result = h.test_result

    print("[Section 1] commands/setup-repo.md exists and holds the decided consent question (#444)")

    doc = read(SETUP_REPO)
    test_result(
        "setup-repo.md exists and is readable",
        doc != "",
        f"path={SETUP_REPO}",
    )
    test_result(
        "direnv consent question is the exact decided text",
        QUESTION in doc,
        f"missing question {QUESTION!r}",
    )

    print()
    print("[Section 2] Branch validation and the generated dotenv line (#444)")

    test_result(
        "Q1 allowlist regex is the one used in sub-step 5a",
        Q1_ALLOWLIST_REGEX in doc,
        f"missing allowlist check {Q1_ALLOWLIST_REGEX!r}",
    )
    test_result(
        "sub-step 5d writes the decided dotenv convention line via that printf format",
        DOTENV_FORMAT in doc and DOTENV_FORMAT.replace("%s", "<b>") == DOTENV_LINE,
        f"missing printf format {DOTENV_FORMAT!r}; it must render to {DOTENV_LINE!r}",
    )
    test_result(
        "no dotenv_if_exists anywhere in setup-repo.md",
        "dotenv_if_exists" not in doc,
        "found dotenv_if_exists; the decided convention line uses an explicit -f test",
    )

    print()
    print("[Section 3] direnv allow runs only on a file this run wrote (#444)")

    allow_at = doc.find(ALLOW_LINE)
    test_result(
        "sub-step 5e contains the direnv allow call on $ENVRC",
        allow_at != -1,
        f"missing {ALLOW_LINE!r}",
    )
    gate_window = doc[max(0, allow_at - 200):allow_at] if allow_at != -1 else ""
    test_result(
        "direnv allow is guarded by ENVRC_STATE = written (not kept or cloned files)",
        WRITTEN_GUARD in gate_window,
        "direnv allow call is not gated on this run having written .envrc",
    )
    test_result(
        "direnv allow is never run on the clone or on ${DEST}",
        not re.search(r'direnv allow "?\$\{?DEST', doc),
        "found direnv allow targeting ${DEST}",
    )

    print()
    print("[Section 4] Hand-off ### direnv block (#444)")

    repo_ready = doc.find("## Repo ready")
    handoff_match = re.search(rf"(?m)^[ \t]*{re.escape(HANDOFF_HEADING)}[ \t]*$", doc)
    handoff_at = handoff_match.start() if handoff_match else -1
    test_result(
        "hand-off template has a ### direnv heading line inside the Repo ready block",
        repo_ready != -1 and handoff_at > repo_ready,
        f"missing {HANDOFF_HEADING!r} heading after '## Repo ready' in the hand-off",
    )
    test_result(
        "hand-off direnv mapping covers the skipped outcome",
        "direnv: skipped" in doc,
        "missing the 'direnv: skipped' status line",
    )
    test_result(
        "hand-off direnv mapping covers the set-up outcome",
        "direnv: set up" in doc,
        "missing the 'direnv: set up' status line",
    )

    print()
    print("[Section 5] No bare positional tokens in the command doc (#444)")

    bare = re.findall(r"\$[0-9]", doc)
    test_result(
        "setup-repo.md has no bare $0-$9 positional tokens",
        not bare,
        f"found bare positional tokens: {sorted(set(bare))}",
    )

    print()
    h.summarize_and_exit()
