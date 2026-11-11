#!/usr/bin/env python3
"""
Doc-content test for the optional direnv step in commands/setup-repo.md (#444).

Every check reads the doc text. A check named "documents ..." confirms the decided wording,
branch or construct is written down in the doc; it does not run the bash or observe behaviour.
Where a check names a construct (a case pattern, a guard, a printf line), the regex or substring
targets that construct, so the name and the assertion describe the same thing.

Run with: python3 tests/test_setup_repo_direnv_doc.py
"""

import re

from _test_harness import REPO_ROOT, Harness

SETUP_REPO_DOC = REPO_ROOT / "commands" / "setup-repo.md"

QUESTION = "Use direnv for environment variables in this project?"
ALLOWLIST_CHARS = "A-Za-z0-9._/-"
DOTENV_FORMAT = 'if [[ -f "%s/.env" ]]; then dotenv "%s/.env"; fi'
# The full printf in 5d: source_up_if_exists first, the unchanged dotenv line, and DEFAULT_BRANCH fed twice.
GENERATED_PRINTF = (
    "printf 'source_up_if_exists\\n" + DOTENV_FORMAT + "\\n' \"$DEFAULT_BRANCH\" \"$DEFAULT_BRANCH\""
)
SKIPPED_UNSAFE = "skipped (unsafe branch name"
INACTIVE_LINE = "direnv: .envrc written but inactive until direnv is installed and hooked"
LOADS_LINE = "direnv: set up — worktrees/.envrc loads <branch>/.env"
UNKNOWN_LINE = "direnv: unknown — check worktrees/.envrc and the exclude entry by hand"

DIRENV_STEP = r"5\.\s*Optional: direnv"
HANDOFF_STEP = r"6\.\s*Confirm and hand off"

BRANCH_CASE_COMMENT = re.compile(r"^[ \t]*# Branch allowlist \(#444\):.*$", re.M)
BARE_POSITIONAL = re.compile(r"\$[0-9](?!\w)")
UNQUOTED_HEREDOC = re.compile(r"<<-?\s*[A-Za-z_]")


def _markdown_section(text, heading):
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


def _branch_case_snippets(section):
    """Each branch-allowlist snippet: from its '# Branch allowlist (#444)' comment line through the
    second `esac` after it, which closes the pair of `case` statements in that block."""
    snippets = []
    for m in BRANCH_CASE_COMMENT.finditer(section):
        esacs = list(re.finditer(r"^[ \t]*esac$", section[m.end():], re.M))
        if len(esacs) < 2:
            snippets.append("")
            continue
        snippets.append(section[m.start(): m.end() + esacs[1].end()])
    return snippets


def _error_row(section, first_cell):
    """The Error Handling table row whose first cell contains `first_cell`, or empty string."""
    for line in section.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells and first_cell in cells[0]:
            return line
    return ""


def main():
    h = Harness("SETUP-REPO DIRENV DOC TEST SUITE")

    exists = SETUP_REPO_DOC.is_file()
    h.test_result("setup-repo doc file exists", exists, str(SETUP_REPO_DOC))
    text = SETUP_REPO_DOC.read_text() if exists else ""
    direnv = _direnv_step(text)
    handoff = _handoff(text)
    # The hand-off template: the '### direnv' line sits inside a code fence in the doc, and this
    # section runs from that line to the Error Handling heading.
    direnv_template = _markdown_section(text, r"direnv\s*$")
    error_handling = _markdown_section(text, r"Error Handling")
    notes = _markdown_section(text, r"Notes")
    frontmatter = _frontmatter(text)

    print()
    print("[Section 1] Placement and numbering")

    h.test_result(
        "doc documents the direnv step titled '5. Optional: direnv'",
        re.search(DIRENV_STEP, text) is not None,
        "step 5 title not found",
    )
    h.test_result(
        "doc documents the hand-off renumbered to '6. Confirm and hand off'",
        re.search(HANDOFF_STEP, text) is not None,
        "step 6 hand-off title not found",
    )
    h.test_result(
        "doc no longer numbers the hand-off '5. Confirm and hand off'",
        re.search(r"5\.\s*Confirm and hand off", text) is None,
        "hand-off still numbered 5",
    )
    gitignore_note = re.search(r"^\W*4a\b", text, re.M)
    h.test_result(
        "doc documents step 4a (project.yaml gitignore note) before the direnv step",
        gitignore_note is not None and bool(direnv) and gitignore_note.start() < text.find(direnv),
        "4a missing or not before step 5",
    )

    print()
    print("[Section 2] The question and its choices")

    h.test_result(
        "doc documents the question text exactly",
        QUESTION in text,
        "question not found",
    )
    h.test_result(
        "doc documents asking the question with AskUserQuestion inside the direnv step",
        QUESTION in direnv and "AskUserQuestion" in direnv,
        "question or AskUserQuestion missing from the direnv step",
    )
    no_idx = text.find("No: skip direnv setup")
    yes_match = re.search(r"Yes:\s*create", text)
    h.test_result(
        "doc documents 'No: skip direnv setup' listed before the Yes option",
        no_idx != -1 and yes_match is not None and no_idx < yes_match.start(),
        "No option missing or not first",
    )
    h.test_result(
        "doc documents the No answer as the hand-off line `direnv: skipped`",
        "`direnv: skipped`" in direnv,
        "`direnv: skipped` not found in the direnv step",
    )
    h.test_result(
        "doc documents the brew install prompt wording 'direnv is not installed. Run `brew install direnv`?'",
        "direnv is not installed. Run `brew install direnv`?" in direnv,
        "install prompt wording not found in the direnv step",
    )
    h.test_result(
        "doc documents printing the direnv install link when brew is missing",
        "https://direnv.net/docs/installation.html" in direnv,
        "install link not found in the direnv step",
    )

    print()
    print("[Section 3] DEFAULT_BRANCH allowlist validation")

    h.test_result(
        "doc documents the allowlist character set 'A-Za-z0-9._/-' in the direnv step",
        ALLOWLIST_CHARS in direnv,
        "allowlist character set not found",
    )
    h.test_result(
        "doc documents the first allowlist case pattern rejecting empty, leading '-' and '..'",
        re.search(r'""\|-\*\|\*\.\.\*\) BRANCH_OK=0', direnv) is not None,
        "case pattern \"\"|-*|*..*) BRANCH_OK=0 not found",
    )
    h.test_result(
        "doc documents the second allowlist case pattern rejecting any character outside the allowlist",
        re.search(r"\*\[!A-Za-z0-9\._/-\]\*\) BRANCH_OK=0", direnv) is not None,
        "case pattern *[!A-Za-z0-9._/-]*) BRANCH_OK=0 not found",
    )
    h.test_result(
        "doc documents the unsafe-name skip line 'direnv: skipped (unsafe branch name ...'",
        SKIPPED_UNSAFE in direnv,
        f"'{SKIPPED_UNSAFE}' not found in the direnv step",
    )
    h.test_result(
        "doc documents step 5a validating DEFAULT_BRANCH via a case on \"$DEFAULT_BRANCH\"",
        re.search(r'case "\$DEFAULT_BRANCH" in', direnv) is not None,
        "case on $DEFAULT_BRANCH not found in the direnv step",
    )
    snippets = _branch_case_snippets(direnv)
    h.test_result(
        "doc documents the 5a and 5c-5e branch-allowlist case snippets as textually identical",
        len(snippets) == 2 and snippets[0] != "" and snippets[0] == snippets[1],
        f"expected two identical snippets, found {len(snippets)} "
        f"(identical: {len(snippets) == 2 and snippets[0] == snippets[1]})",
    )
    h.test_result(
        "doc documents the 5a result key as DIRENV_PRECHECK",
        direnv.count("printf 'DIRENV_PRECHECK=%s\\n'") == 1,
        "printf 'DIRENV_PRECHECK=%s' not found exactly once",
    )

    print()
    print("[Section 4] .env: excluded from git, then created")

    h.test_result(
        "doc documents the tracked-.env probe as 'ls-files --error-unmatch -- .env'",
        "ls-files --error-unmatch -- .env" in direnv,
        "tracked-.env probe not found",
    )
    h.test_result(
        "doc documents the already-ignored probe as 'check-ignore -q -- .env'",
        "check-ignore -q -- .env" in direnv,
        "already-ignored probe not found",
    )
    h.test_result(
        "doc documents the exclude file resolved through 'rev-parse --git-path info/exclude'",
        "rev-parse --git-path info/exclude" in direnv,
        "rev-parse --git-path info/exclude not found",
    )
    h.test_result(
        "doc documents the exclude entry as the clone-anchored '/.env' line",
        re.search(r"printf '/\.env\\n' >> \"\$EXCLUDE\"", direnv) is not None,
        "printf '/.env' append to $EXCLUDE not found",
    )
    h.test_result(
        "doc documents the exclude append guarded by grep -qxF on the same '/.env' string",
        re.search(r"grep -qxF '/\.env'", direnv) is not None,
        "grep -qxF '/.env' guard not found",
    )
    h.test_result(
        "doc documents .env created under umask 077 (mode 600)",
        re.search(r'umask 077; : > "\$\{DEST\}/\.env"', direnv) is not None,
        "umask 077 .env creation not found",
    )
    h.test_result(
        "doc documents .env created only when untracked, ignored, and absent (no file or symlink)",
        re.search(r'\[ ! -e "\$\{DEST\}/\.env" \] && \[ ! -L "\$\{DEST\}/\.env" \]', direnv) is not None,
        "absent-and-not-symlink guard on .env not found",
    )

    print()
    print("[Section 5] worktrees/.envrc: generated lines, no-clobber, shadowing")

    h.test_result(
        "doc documents the generated dotenv line in the quoted DOTENV_FORMAT",
        DOTENV_FORMAT in direnv,
        f"'{DOTENV_FORMAT}' not found in the direnv step",
    )
    h.test_result(
        "doc documents the generated .envrc as source_up_if_exists, then the dotenv line, both fed DEFAULT_BRANCH",
        GENERATED_PRINTF in direnv,
        "generated printf with source_up_if_exists first and DEFAULT_BRANCH twice not found",
    )
    h.test_result(
        "doc documents no dotenv or printf line hardcoding main/.env (branch comes from DEFAULT_BRANCH)",
        not any(
            "main/.env" in line
            for line in text.splitlines()
            if "dotenv" in line or "printf" in line
        ),
        "a dotenv or printf line hardcodes main/.env",
    )
    h.test_result(
        "doc documents the generated dotenv line as not dotenv_if_exists",
        not any(
            "dotenv_if_exists" in line and ("printf" in line or ">" in line)
            for line in text.splitlines()
        ),
        "dotenv_if_exists appears in a line that writes the .envrc",
    )
    h.test_result(
        "doc documents the .envrc write under set -o noclobber, redirected to \"$ENVRC\"",
        re.search(r'set -o noclobber; printf [^\n]*> "\$ENVRC" \)', direnv) is not None,
        "noclobber .envrc write not found",
    )
    h.test_result(
        "doc documents the .envrc existence guard covering symlinks ([ -L \"$ENVRC\" ])",
        '[ -L "$ENVRC" ]' in direnv,
        "[ -L \"$ENVRC\" ] guard not found",
    )
    h.test_result(
        "doc documents the never-overwrite rule for an existing .envrc",
        bool(re.search(r"(?i)never overwrite", direnv)),
        "'never overwrite' wording not found",
    )
    h.test_result(
        "doc documents the .envrc body is not written with an unquoted heredoc",
        not any(
            UNQUOTED_HEREDOC.search(line)
            for line in text.splitlines()
            if "envrc" in line.lower()
        )
        and not UNQUOTED_HEREDOC.search(direnv),
        "unquoted heredoc found on a line touching .envrc or in the direnv step",
    )
    h.test_result(
        "doc documents the shadow probe on the clone's own ${DEST}/.envrc",
        re.search(r'\[ -e "\$\{DEST\}/\.envrc" \]', direnv) is not None,
        "${DEST}/.envrc shadow probe not found",
    )
    h.test_result(
        "doc documents the shadowing hand-off line 'shadows worktrees/.envrc; add source_up_if_exists to it'",
        "shadows worktrees/.envrc; add source_up_if_exists to it" in direnv_template,
        "shadowing hand-off line not found in the hand-off template",
    )
    h.test_result(
        "doc documents that DIRENV_VALIDATED has exactly one producer, the 5c-5e printf",
        direnv.count("printf 'DIRENV_VALIDATED=%s") == 1
        and direnv.find("printf 'DIRENV_VALIDATED=%s") > direnv.find("5c to 5e."),
        "DIRENV_VALIDATED is not printed exactly once, inside 5c to 5e",
    )
    h.test_result(
        "doc documents DIRENV_AVAILABLE derived by command -v direnv inside 5c to 5e",
        re.search(r"DIRENV_AVAILABLE=0\n\s*command -v direnv", direnv) is not None,
        "DIRENV_AVAILABLE=0 followed by command -v direnv not found",
    )
    h.test_result(
        "doc documents that DIRENV_AVAILABLE is not carried from 5b",
        "`DIRENV_AVAILABLE` is not carried" in direnv,
        "'`DIRENV_AVAILABLE` is not carried' note not found",
    )
    h.test_result(
        "doc documents the DIRENV_DONE=1 sentinel as the last line the 5c-5e block prints",
        "DIRENV_DONE=1\\n'" in direnv,
        "DIRENV_DONE=1 sentinel printf not found",
    )
    h.test_result(
        "doc documents DIRENV_ALLOW as a printed key of the 5c-5e block",
        "DIRENV_ALLOW=%s" in direnv,
        "DIRENV_ALLOW=%s not printed by the 5c-5e block",
    )

    print()
    print("[Section 6] direnv allow gating")

    h.test_result(
        "doc documents direnv allow only inside the ENVRC_STATE = written guard, withheld when TRACKED=1",
        re.search(
            r'if \[ "\$ENVRC_STATE" = written \] && \[ "\$DIRENV_AVAILABLE" = 1 \]; then\s+'
            r'if \[ "\$TRACKED" -eq 1 \]; then\s+ALLOW_STATE=withheld-tracked-env\s+'
            r'elif direnv allow "\$ENVRC" >/dev/null 2>&1; then',
            direnv,
        ) is not None,
        "direnv allow is not guarded by ENVRC_STATE = written, or is not withheld when TRACKED=1",
    )
    h.test_result(
        "doc documents exactly one direnv allow call, on \"$ENVRC\"",
        direnv.count('direnv allow "$ENVRC"') == 1,
        "direnv allow \"$ENVRC\" does not appear exactly once",
    )
    h.test_result(
        "doc documents ALLOW_STATE=ok and ALLOW_STATE=failed for a successful and a failed allow",
        "ALLOW_STATE=ok" in direnv and "ALLOW_STATE=failed" in direnv,
        "ALLOW_STATE=ok or ALLOW_STATE=failed not set in the direnv step",
    )
    h.test_result(
        "doc documents the kept-existing hand-off line with review before the allow command",
        "direnv: kept existing worktrees/.envrc — review it, then run: direnv allow" in direnv_template,
        "kept-existing hand-off line not found in the hand-off template",
    )

    print()
    print("[Section 7] Hand-off direnv template")

    h.test_result(
        "doc documents a '### direnv' line after the 'Confirm and hand off' step",
        bool(re.search(r"^###\s+direnv\s*$", handoff, re.M)),
        "'### direnv' line not found after the hand-off step",
    )
    h.test_result(
        "doc documents the set-up hand-off line 'direnv: set up — worktrees/.envrc loads <branch>/.env'",
        LOADS_LINE in direnv_template,
        "set-up hand-off line not found in the hand-off template",
    )
    h.test_result(
        "doc documents the inactive hand-off line for a written .envrc without direnv",
        INACTIVE_LINE in direnv_template,
        "inactive-until-installed hand-off line not found in the hand-off template",
    )
    h.test_result(
        "doc documents the withheld hand-off line for DIRENV_ALLOW=withheld-tracked-env",
        "DIRENV_ALLOW=withheld-tracked-env" in direnv_template
        and "direnv: .envrc written, but direnv allow withheld — .env is tracked by this repo; "
        "review the tracked .env first" in direnv_template,
        "withheld-tracked-env hand-off line or its review-first wording not found",
    )
    h.test_result(
        "doc documents the allow-failed hand-off line 'direnv: .envrc written, but direnv allow failed'",
        "direnv: .envrc written, but direnv allow failed — run: direnv allow" in direnv_template,
        "allow-failed hand-off line not found in the hand-off template",
    )
    h.test_result(
        "doc documents the unknown fallback line, gated on DIRENV_DONE=1 and DIRENV_ENVRC",
        UNKNOWN_LINE in direnv_template
        and "both `DIRENV_DONE=1` and `DIRENV_ENVRC`" in direnv_template,
        "unknown fallback line or the DIRENV_DONE=1 trust rule not found",
    )
    h.test_result(
        "doc documents the secrets follow-up '(git-ignored, mode 600)' for an untracked .env",
        "(git-ignored, mode 600)" in direnv_template,
        "'(git-ignored, mode 600)' secrets line not found in the hand-off template",
    )
    h.test_result(
        "doc documents the secrets follow-up warning when .env is tracked",
        ".env is tracked by this repo; keep secrets out of it" in direnv_template,
        "tracked-.env warning not found in the hand-off template",
    )
    h.test_result(
        "doc documents the read-only hook check: 'direnv hook zsh' grepped in ~/.zshrc and ~/.zshenv, never edited",
        'for f in "$HOME/.zshrc" "$HOME/.zshenv"; do' in direnv_template
        and "grep -q 'direnv hook zsh' \"$f\"" in direnv_template
        and "never edit" in direnv_template,
        "hook-check loop over both startup files, its grep, or the never-edit rule not found",
    )
    h.test_result(
        "doc documents the ADR-0014 pointer naming COMPOSE_PROJECT_NAME and the worktree-reference.md section",
        "Compose COMPOSE_PROJECT_NAME: see ADR-0014" in direnv_template
        and "~/.claude/prompts/worktree-reference.md (Docker Compose Project Isolation)" in direnv_template,
        "ADR-0014 pointer or worktree-reference.md pointer not found",
    )
    h.test_result(
        "doc documents the ADR-0021 pointer for the merge-queue config reading the main worktree's .envrc",
        "The merge-queue config reads the main worktree's .envrc: see ADR-0021" in direnv_template,
        "ADR-0021 merge-queue pointer not found",
    )

    print()
    print("[Section 8] Error Handling rows and Notes")

    h.test_result(
        "doc documents an Error Handling section",
        error_handling != "",
        "no 'Error Handling' heading found",
    )
    row_checks = [
        ("Unsafe `DEFAULT_BRANCH`", "direnv: skipped (unsafe branch name", "unsafe branch"),
        ("`brew` missing", "https://direnv.net/docs/installation.html", "brew missing"),
        ("exists or is a symlink", "never follow the symlink", "symlink"),
        ("Clone has its own `.envrc`", "source_up_if_exists", "shadowing"),
        ("`.env` is tracked by the clone", "DIRENV_ALLOW=withheld-tracked-env", "tracked .env"),
        ("`direnv allow` fails", "direnv allow <path>", "allow failure"),
        ("Block result missing `DIRENV_DONE=1`", "direnv: unknown", "missing sentinel"),
    ]
    for first_cell, must_contain, label in row_checks:
        row = _error_row(error_handling, first_cell)
        h.test_result(
            f"doc documents an Error Handling row for {label} containing '{must_contain}'",
            must_contain in row,
            f"row with first cell '{first_cell}' missing or lacks '{must_contain}'",
        )
    h.test_result(
        "doc documents the Notes mention of the worktrees/.envrc location beside the default-branch directory",
        "worktrees/.envrc" in notes,
        "Notes section does not mention worktrees/.envrc",
    )

    print()
    print("[Section 9] Frontmatter description")

    h.test_result(
        "doc has leading YAML frontmatter",
        frontmatter != "",
        "no leading YAML frontmatter block",
    )
    h.test_result(
        "doc documents the optional direnv setup in the frontmatter description",
        "optional direnv setup" in frontmatter,
        "'optional direnv setup' not in the frontmatter",
    )
    h.test_result(
        "doc documents in the frontmatter that an existing .envrc is never overwritten",
        "an existing `.envrc` is never overwritten" in frontmatter,
        "never-overwrite clause not in the frontmatter",
    )

    print()
    print("[Section 10] Shell conventions")

    bare = [
        f"line {lineno}: {line.strip()}"
        for lineno, line in enumerate(text.splitlines(), start=1)
        if BARE_POSITIONAL.search(line)
    ]
    h.test_result(
        "doc contains no bare positional $0-$9 token in commands/setup-repo.md",
        not bare,
        "\n      " + "\n      ".join(bare) if bare else "",
    )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
