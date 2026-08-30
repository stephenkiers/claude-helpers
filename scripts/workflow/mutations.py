"""
Mutation allowlist: centralized gating for destructive git and gh operations.

The mutation funnel is the single choke point for all destructive git and gh
operations this module builds directly. It enforces that only exact argument
shapes in the allowlist are permitted, rejecting anything not explicitly
allowed (including attempts to smuggle extra flags). Placeholder values
(e.g. "<path>", "<name>") never match an operand starting with "-", and the
argv builders in git.py insert a literal "--" end-of-options separator
immediately before each *positional* placeholder value to keep a
malicious-looking value from being parsed as a flag even if it slipped past
that check. Placeholders that are instead consumed as a flag's mandatory
argument (e.g. "-F <path>") never need "--": git already takes the very
next token as that flag's value, dash-prefixed or not, so inserting "--"
there would itself misparse — the "--" would become -F's argument and
<path> would fall through as a stray positional pathspec.

Subcommand keys (per ADR-0013 and Amendment 2): "worktree", "branch", "pull"
(git), "add", "commit", "push" (git, /shipit), "pr" (gh, including /shipit), and
"issue" (gh, Phase 3a /track-and-start).

Decision 1 (ADR-0013): Every mutation operation must pass through
check_mutation_allowed() — the CLI never constructs a mutating call directly.
"""

from typing import List, Tuple, Optional


MUTATION_ALLOWLIST = {
    "worktree": {
        ("remove", "--", "<path>"): "git worktree remove -- <path>",
        ("remove", "--force", "--", "<path>"): "git worktree remove --force -- <path>",
        ("add", "-b", "<branch>", "--", "<path>"): "git worktree add -b <branch> -- <path>",
        ("add", "-b", "<branch>", "--", "<path>", "<base>"): "git worktree add -b <branch> -- <path> <base>",
    },
    "branch": {
        ("-d", "--", "<name>"): "git branch -d -- <name>",
        ("-D", "--", "<name>"): "git branch -D -- <name>",
    },
    "pull": {
        ("--ff-only", "--", "<remote>", "<branch>"): "git pull --ff-only -- <remote> <branch>",
    },
    "add": {
        ("-A",): "git add -A",
    },
    "commit": {
        ("-F", "<path>"): "git commit -F <path>",
    },
    "push": {
        ("-u", "<remote>", "<branch>"): "git push -u <remote> <branch>",
    },
    "pr": {
        ("merge", "--squash", "<pr_number>"): "gh pr merge --squash <pr_number>",
        ("create", "--title", "<title>", "--body-file", "<path>"): "gh pr create --title <title> --body-file <path>",
        ("create", "--title", "<title>", "--base", "<branch>", "--body-file", "<path>"): "gh pr create --title <title> --base <branch> --body-file <path>",
        ("edit", "<pr_number>", "--title", "<title>", "--body-file", "<path>"): "gh pr edit <pr_number> --title <title> --body-file <path>",
    },
    "issue": {
        ("create", "--title", "<title>", "--body-file", "<path>"): "gh issue create --title <title> --body-file <path>",
        ("create", "--title", "<title>", "--label", "<labels>", "--body-file", "<path>"): "gh issue create --title <title> --label <labels> --body-file <path>",
        ("create", "--title", "<title>", "--assignee", "<assignee>", "--body-file", "<path>"): "gh issue create --title <title> --assignee <assignee> --body-file <path>",
        ("create", "--title", "<title>", "--label", "<labels>", "--assignee", "<assignee>", "--body-file", "<path>"): "gh issue create --title <title> --label <labels> --assignee <assignee> --body-file <path>",
        ("comment", "<number>", "--body-file", "<path>"): "gh issue comment <number> --body-file <path>",
        ("edit", "<number>", "--body-file", "<path>"): "gh issue edit <number> --body-file <path>",
    },
}


def check_mutation_allowed(args: List[str]) -> Tuple[bool, Optional[str]]:
    """
    Check if a git subcommand + arguments match an allowlist entry.

    Args:
        args: Full git command arguments (subcommand + operands, e.g. ["worktree", "remove", "/path"])

    Returns:
        (True, None) if exact match found in allowlist.
        (False, "<reason>") if not allowed or doesn't match any known pattern.

    Never returns a partial match or best-guess — rejects anything not explicitly
    allowlisted, including attempts to smuggle extra flags or arguments.
    """
    if not args or len(args) < 2:
        return False, "mutation must have at least subcommand + one argument"

    subcommand = args[0]
    operands = args[1:]

    if subcommand not in MUTATION_ALLOWLIST:
        return False, f"subcommand '{subcommand}' is not in mutation allowlist"

    allowed_shapes = MUTATION_ALLOWLIST[subcommand]

    for shape, description in allowed_shapes.items():
        if _matches_shape(operands, shape):
            return True, None

    return False, f"argument shape for '{subcommand}' does not match any allowed pattern (got {operands})"


def _matches_shape(operands: List[str], shape: Tuple[str, ...]) -> bool:
    """
    Check if operands match a shape exactly.

    A shape is a tuple of literal flags/subcommands and placeholders like "<path>" or "<name>".
    "<...>" placeholders match any non-empty string (except those starting with "-", which could
    be interpreted as flags); literals must match byte-for-byte.

    Example:
        _matches_shape(["remove", "/tmp/wt"], ("remove", "<path>")) → True
        _matches_shape(["remove", "--force", "/tmp/wt"], ("remove", "<path>")) → False (extra arg)
        _matches_shape(["remove", "--evil"], ("remove", "<path>")) → False (placeholder starts with -)
    """
    if len(operands) != len(shape):
        return False

    for operand, pattern in zip(operands, shape):
        if pattern.startswith("<") and pattern.endswith(">"):
            if not operand or operand.startswith("-"):
                return False
        else:
            if operand != pattern:
                return False

    return True
