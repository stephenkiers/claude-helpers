"""
Mutation allowlist: centralized gating for destructive git operations.

The mutation funnel is the single choke point for all destructive git operations.
It enforces that only exact argument shapes in the allowlist are permitted,
rejecting anything not explicitly allowed (including attempts to smuggle extra flags).

Decision 1 (ADR-0013): Every mutation operation must pass through
check_mutation_allowed() — the CLI never constructs a mutating call directly.
"""

from typing import List, Tuple, Optional


MUTATION_ALLOWLIST = {
    "worktree": {
        ("remove", "<path>"): "git worktree remove <path>",
        ("remove", "--force", "<path>"): "git worktree remove --force <path>",
    },
    "branch": {
        ("-d", "<name>"): "git branch -d <name>",
        ("-D", "<name>"): "git branch -D <name>",
    },
    "pull": {
        ("--ff-only", "<remote>", "<branch>"): "git pull --ff-only <remote> <branch>",
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
    "<...>" placeholders match any non-empty string; literals must match byte-for-byte.

    Example:
        _matches_shape(["remove", "/tmp/wt"], ("remove", "<path>")) → True
        _matches_shape(["remove", "--force", "/tmp/wt"], ("remove", "<path>")) → False (extra arg)
    """
    if len(operands) != len(shape):
        return False

    for operand, pattern in zip(operands, shape):
        if pattern.startswith("<") and pattern.endswith(">"):
            if not operand:
                return False
        else:
            if operand != pattern:
                return False

    return True
