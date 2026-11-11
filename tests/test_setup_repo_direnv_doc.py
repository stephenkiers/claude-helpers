#!/usr/bin/env python3
"""
Doc-content test for the optional direnv step in commands/setup-repo.md.

Plan: add-optional-direnv-setup-to-setup-repo. Asserts the decided wording and branches are
visible in the doc: the question, the DEFAULT_BRANCH allowlist (Q1), the literal dotenv
convention line (no dotenv_if_exists), `direnv allow` only on a file written in this run,
the `.env` exclude and mode-600 handling, the no-clobber `.envrc` write, the hand-off
`### direnv` block, the Error Handling rows, the frontmatter clause, and the shell conventions.

The doc is read at run time by this file. Checks are substring or regex checks over that text;
they confirm the decided branches are written down, not that the bash behaves.

Run with: python3 tests/test_setup_repo_direnv_doc.py
"""

import re

from _test_harness import REPO_ROOT, Harness

SETUP_REPO_DOC = REPO_ROOT / "commands" / "setup-repo.md"

QUESTION = "Use direnv for environment variables in this project?"
ALLOWLIST_CHARS = "A-Za-z0-9._/-"
DOTENV_FORMAT = 'if [[ -f "%s/.env" ]]; then dotenv "%s/.env"; fi'
SKIPPED_UNSAFE = "skipped (unsafe branch name"
INACTIVE_LINE = "inactive until direnv is installed and hooked"

DIRENV_STEP = r"5\.\s*Optional: direnv"
HANDOFF_STEP = r"6\.\s*Confirm and hand off"

BARE_POSITIONAL = re.compile(r"\$[0-9](?!\w)")
UNQUOTED_HEREDOC = re.compile(r"<<-?\s*[A-Za-z_]")


def _section_after(text, heading):
    """Body of the markdown section whose heading line matches `heading` (a regex), up to the
    next heading at the same or a higher level. Empty string when the heading is absent."""
    m = re.search(r"^(#+)\s*" + heading + r".*$", text, re.M)
    if not m:
        return ""
    level = len(m.group(1))
    nxt = re.search(r"^#{1," + str(level) + r"}\s", text[m.end():], re.M)
    return text[m.end(): m.end() + nxt.start()] if nxt else text[m.end():]


def _direnv_step(text):
    """From the step 5 'Optional: direnv' title up to the 'Confirm and hand off' step title.
    Empty string when the two titles are missing or out of order."""
    start = re.search(DIRENV_STEP, text)
    end = re.search(HANDOFF_STEP, text)
    if not start or not end or end.start() <= start.start():
        return ""
    return text[start.start(): end.start()]


def _handoff(text):
    """From the step 6 'Confirm and hand off' title to the end of the doc."""
    m = re.search(HANDOFF_STEP, text)
    return text[m.start():] if m else ""


def _frontmatter(text):
    """The YAML frontmatter block between the leading '---' fences, or empty string."""
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---", 4)
    return text[4:end] if end != -1 else ""


def _allow_near_written_run(section):
    """True when some 'direnv allow' mention sits within three lines of 'written in this run'
    (or 'written this run')."""
    lines = section.splitlines()
    for i, line in enumerate(lines):
        if "direnv allow" not in line:
            continue
        window = "\n".join(lines[max(0, i - 3): i + 4])
        if re.search(r"written (in this|this) run", window, re.I):
            return True
    return False


def main():
    h = Harness("SETUP-REPO DIRENV DOC TEST SUITE")

    exists = SETUP_REPO_DOC.is_file()
    h.test_result("commands/setup-repo.md exists", exists, str(SETUP_REPO_DOC))
    text = SETUP_REPO_DOC.read_text() if exists else ""
    direnv = _direnv_step(text)
    handoff = _handoff(text)
    direnv_block = _section_after(text, r"direnv\s*$")
    error_handling = _section_after(text, r"Error Handling")
    notes = _section_after(text, r"Notes")
    frontmatter = _frontmatter(text)

    print()
    print("[Section 1] Placement, numbering and order")

    h.test_result(
        "direnv step is titled '5. Optional: direnv'",
        re.search(DIRENV_STEP, text) is not None,
        "step 5 title not found",
    )
    h.test_result(
        "hand-off is renumbered to '6. Confirm and hand off'",
        re.search(HANDOFF_STEP, text) is not None,
        "step 6 hand-off title not found",
    )
    h.test_result(
        "old hand-off numbering '5. Confirm and hand off' is gone",
        re.search(r"5\.\s*Confirm and hand off", text) is None,
        "hand-off still numbered 5",
    )
    gitignore_note = re.search(r"^\W*4a\b", text, re.M)
    h.test_result(
        "step 4a (project.yaml gitignore note) is present before the direnv step",
        gitignore_note is not None and bool(direnv) and gitignore_note.start() < text.find(direnv),
        "4a missing or not before step 5",
    )
    h.test_result(
        "direnv step sits before the hand-off step",
        bool(direnv) and bool(handoff),
        "direnv step or hand-off step missing, or out of order",
    )

    print()
    print("[Section 2] The question and its choices")

    h.test_result(
        "question text is exactly 'Use direnv for environment variables in this project?'",
        QUESTION in text,
        "question not found",
    )
    h.test_result(
        "the question is asked inside the direnv step via AskUserQuestion",
        QUESTION in direnv and "AskUserQuestion" in direnv,
        "question or AskUserQuestion missing from the direnv step",
    )
    no_idx = text.find("No: skip direnv setup")
    yes_match = re.search(r"Yes:\s*create", text)
    h.test_result(
        "'No: skip direnv setup' option is present and listed before the Yes option",
        no_idx != -1 and yes_match is not None and no_idx < yes_match.start(),
        "No option missing or not first",
    )
    h.test_result(
        "No answer prints the exact line 'direnv: skipped'",
        "direnv: skipped" in text,
        "'direnv: skipped' not found",
    )
    h.test_result(
        "brew install prompt uses the decided wording 'direnv is not installed. Run `brew install direnv`?'",
        "direnv is not installed. Run `brew install direnv`?" in direnv,
        "install prompt wording not found in the direnv step",
    )
    h.test_result(
        "brew-missing path prints the direnv install link",
        "https://direnv.net/docs/installation.html" in direnv,
        "install link not found in the direnv step",
    )

    print()
    print("[Section 3] Q1: DEFAULT_BRANCH allowlist validation")

    h.test_result(
        "allowlist character set 'A-Za-z0-9._/-' is named in the direnv step",
        ALLOWLIST_CHARS in direnv,
        "allowlist character set not found",
    )
    h.test_result(
        "the direnv step rejects '..' in the branch name",
        ".." in direnv and bool(re.search(r"(?i)reject|unsafe|not allow|must not", direnv)),
        "'..' rejection not described in the direnv step",
    )
    h.test_result(
        "the direnv step rejects a branch name starting with '-'",
        bool(re.search(r"(?i)(starting|start) with\s*`?-", direnv)),
        "leading '-' rejection not described in the direnv step",
    )
    h.test_result(
        "unsafe-name skip prints 'direnv: skipped (unsafe branch name ...'",
        SKIPPED_UNSAFE in text,
        f"'{SKIPPED_UNSAFE}' not found",
    )
    h.test_result(
        "the validation is applied to DEFAULT_BRANCH",
        "DEFAULT_BRANCH" in direnv,
        "DEFAULT_BRANCH not referenced in the direnv step",
    )

    print()
    print("[Section 4] .env: excluded from git, then created")

    h.test_result(
        "tracked-.env probe uses 'ls-files --error-unmatch'",
        "ls-files --error-unmatch" in direnv,
        "ls-files --error-unmatch not found",
    )
    h.test_result(
        "already-ignored probe uses 'check-ignore -q'",
        "check-ignore -q" in direnv,
        "check-ignore -q not found",
    )
    h.test_result(
        "exclude entry goes through 'info/exclude' resolved by 'rev-parse --git-path'",
        "info/exclude" in direnv and "rev-parse --git-path" in direnv,
        "info/exclude or rev-parse --git-path missing",
    )
    h.test_result(
        "exclude append is guarded by 'grep -qxF' so it is not duplicated",
        "grep -qxF" in direnv,
        "grep -qxF not found",
    )
    h.test_result(
        "created .env gets mode 600 (umask 077 or chmod 600)",
        bool(re.search(r"umask 077|chmod 600", direnv)),
        "mode 600 handling not found",
    )

    print()
    print("[Section 5] worktrees/.envrc: generated line, no-clobber, shadowing")

    h.test_result(
        "generated dotenv line uses the decided quoted convention",
        DOTENV_FORMAT in direnv,
        f"'{DOTENV_FORMAT}' not found in the direnv step",
    )
    h.test_result(
        "no dotenv line hardcodes 'main/.env' (branch comes from DEFAULT_BRANCH)",
        not any(
            "main/.env" in line
            for line in text.splitlines()
            if "dotenv" in line or "printf" in line
        ),
        "a dotenv or printf line hardcodes main/.env",
    )
    h.test_result(
        "generated dotenv line is not dotenv_if_exists",
        not any(
            "dotenv_if_exists" in line and ("printf" in line or ">" in line)
            for line in text.splitlines()
        ),
        "dotenv_if_exists appears in a line that writes the .envrc",
    )
    h.test_result(
        "the printf format is followed by DEFAULT_BRANCH arguments",
        DOTENV_FORMAT in direnv
        and "DEFAULT_BRANCH" in direnv[direnv.find(DOTENV_FORMAT): direnv.find(DOTENV_FORMAT) + 300],
        "DEFAULT_BRANCH not passed to the dotenv printf",
    )
    h.test_result(
        "the .envrc write is no-clobber (noclobber)",
        "noclobber" in direnv,
        "noclobber not found",
    )
    h.test_result(
        "the .envrc existence guard covers symlinks ('[ -L')",
        "[ -L" in direnv,
        "[ -L guard not found",
    )
    h.test_result(
        "the doc states an existing .envrc is never overwritten",
        bool(re.search(r"(?i)never overwrite", direnv)),
        "'never overwrite' wording not found",
    )
    h.test_result(
        "the .envrc body is not written with an unquoted heredoc",
        not any(
            UNQUOTED_HEREDOC.search(line)
            for line in text.splitlines()
            if "envrc" in line.lower()
        )
        and not UNQUOTED_HEREDOC.search(direnv),
        "unquoted heredoc found on a line touching .envrc or in the direnv step",
    )
    h.test_result(
        "shadowing probe checks the clone's own '${DEST}/.envrc'",
        "${DEST}/.envrc" in direnv,
        "${DEST}/.envrc probe not found",
    )
    h.test_result(
        "shadowing is reported with 'shadows worktrees/.envrc' and the source_up_if_exists fix",
        "shadows worktrees/.envrc" in text and "source_up_if_exists" in text,
        "shadowing wording or source_up_if_exists fix missing",
    )

    print()
    print("[Section 6] direnv allow gating (W1)")

    h.test_result(
        "the doc contains a 'direnv allow' step",
        "direnv allow" in direnv,
        "'direnv allow' not found in the direnv step",
    )
    h.test_result(
        "'direnv allow' is stated to run only on a file written in this run",
        _allow_near_written_run(direnv),
        "no 'direnv allow' mention sits next to 'written in this run'",
    )
    h.test_result(
        "a kept existing .envrc prints 'kept existing' and asks for review before the allow command",
        "kept existing" in direnv and bool(re.search(r"(?i)review", direnv)),
        "'kept existing' with review note not found",
    )
    h.test_result(
        "a failed direnv allow is reported as 'direnv allow failed' in the hand-off",
        "direnv allow failed" in text,
        "'direnv allow failed' not found",
    )

    print()
    print("[Section 7] Hand-off direnv block")

    h.test_result(
        "hand-off has a '### direnv' block after the 'Confirm and hand off' step",
        bool(re.search(r"^###\s+direnv\s*$", handoff, re.M)),
        "'### direnv' heading not found after the hand-off step",
    )
    h.test_result(
        "hand-off block states 'direnv: set up' with the loads line",
        "direnv: set up" in direnv_block and "loads" in direnv_block,
        "'direnv: set up ... loads' line not in the direnv block",
    )
    h.test_result(
        "hand-off block states 'direnv: kept existing worktrees/.envrc'",
        "direnv: kept existing worktrees/.envrc" in direnv_block,
        "'direnv: kept existing worktrees/.envrc' line not in the direnv block",
    )
    h.test_result(
        "hand-off block states the inactive line for a written .envrc without direnv",
        "direnv: .envrc written" in direnv_block and INACTIVE_LINE in direnv_block,
        "inactive-until-installed line not in the direnv block",
    )
    h.test_result(
        "hand-off block states 'direnv: .envrc written, but direnv allow failed'",
        "direnv: .envrc written, but direnv allow failed" in direnv_block,
        "allow-failed line not in the direnv block",
    )
    h.test_result(
        "secrets follow-up says 'mode 600' for an untracked .env",
        "mode 600" in direnv_block,
        "'mode 600' secrets line not in the direnv block",
    )
    h.test_result(
        "secrets follow-up warns when .env is tracked ('keep secrets out of it')",
        "is tracked by this repo; keep secrets out of it" in direnv_block,
        "tracked-.env warning not in the direnv block",
    )
    h.test_result(
        "hook check looks for 'direnv hook zsh' in ~/.zshrc and ~/.zshenv, and never edits",
        "direnv hook zsh" in direnv_block
        and "~/.zshrc" in direnv_block
        and "~/.zshenv" in direnv_block
        and bool(re.search(r"(?i)never edit", direnv_block)),
        "hook check, both startup files, or the never-edit rule missing",
    )
    h.test_result(
        "ADR-0014 line names COMPOSE_PROJECT_NAME and merge-queue config as hand-added blocks",
        "ADR-0014" in direnv_block
        and "COMPOSE_PROJECT_NAME" in direnv_block
        and "merge-queue" in direnv_block,
        "ADR-0014 pointer or the named project blocks missing",
    )

    print()
    print("[Section 8] Error Handling rows and Notes")

    h.test_result(
        "Error Handling section exists",
        error_handling != "",
        "no 'Error Handling' heading found",
    )
    missing_rows = [
        kw for kw in ("unsafe", "brew", "symlink", "shadow", "tracked", "direnv allow")
        if kw not in error_handling
    ]
    h.test_result(
        "Error Handling covers unsafe branch, brew, symlink, shadowing, tracked .env and allow failure",
        bool(error_handling) and not missing_rows,
        f"missing keywords: {missing_rows}" if missing_rows else "",
    )
    h.test_result(
        "Notes mention the worktrees/.envrc location beside the default-branch directory",
        "worktrees/.envrc" in notes,
        "Notes section does not mention worktrees/.envrc",
    )

    print()
    print("[Section 9] Frontmatter description")

    h.test_result(
        "frontmatter exists",
        frontmatter != "",
        "no leading YAML frontmatter block",
    )
    h.test_result(
        "frontmatter description has one clause on the optional direnv step",
        bool(re.search(r"(?i)direnv", frontmatter)),
        "'direnv' not in the frontmatter",
    )

    print()
    print("[Section 10] Shell conventions")

    bare = [
        f"line {lineno}: {line.strip()}"
        for lineno, line in enumerate(text.splitlines(), start=1)
        if BARE_POSITIONAL.search(line)
    ]
    h.test_result(
        "no bare positional $0-$9 token anywhere in commands/setup-repo.md",
        not bare,
        "\n      " + "\n      ".join(bare) if bare else "",
    )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
