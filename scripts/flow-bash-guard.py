#!/usr/bin/env python3
"""
PreToolUse hook for flow-implement, flow-review, flow-ship, and flow-merge agents:
best-effort Bash command guard with per-agent rules.

Threat model: prevent accidental shell wrappers and publishing commands.
Best-effort control, not a sandbox: write-a-script-then-run, a nested general-purpose agent,
and the residual gaps listed in tests/test_flow_bash_guard_quotes.py (FALSE_NEGATIVE_LEDGER)
bypass it. See docs/adr/0008.

This script refuses:
  - Top-level shell wrappers (sh -c, bash -c, eval, xargs git/gh, env ... git/gh, etc.)
  - Publishing verbs (git push, gh pr create, gh pr merge, etc.), including when they are
    reached through a transparent prefix (command, exec, nohup, time, timeout N, nice -n N,
    VAR=value, =name) or inside a python/node/perl/ruby -c/-e program
  - Commands it cannot follow: unterminated quotes, unclosed $( or backticks, nesting deeper
    than 64 groups

Splitting is quote-aware: separators inside '...' and "..." are literal, while $(...),
`...`, (...) and {...} are recursed into, so a publishing verb inside a group is still seen.
A heredoc (<<) or a word-start # comment switches to a quote-blind split, which is the
conservative pre-change behaviour, because those constructs can desynchronise the quote
tracking.

Per-agent behavior:
  - flow-implement: full wrapper and publishing-verb checks (existing behavior)
  - flow-review: pass-through (allow all Bash commands)
  - flow-ship: restricted wrappers, allowed python3/eval forms, restricted git push forms,
    gh stack sync allowed, gh pr merge (any flags) denied
  - flow-merge: restricted wrappers, allowed python3/eval forms, allowed git push forms,
    gh stack sync allowed, gh pr merge --admin denied, gh pr merge --squash allowed only
    when no merge-queue config exists

Exit codes:
  - 0: allow (command is safe)
  - 2: block (command violates a rule or is unparseable)
"""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple, List

# Known flow agents that are governed by this guard.
KNOWN_FLOW_AGENTS = ("flow-implement", "flow-review", "flow-ship", "flow-merge", "flow-healer")

# Escalation hint for denied push/merge commands: tells the agent to surface via Ask block.
ESCALATION_HINT = "If this command is legitimate, do not retry a variant: surface it to the human through the Ask block (prompts/flow-reference.md) instead."

# The wrapper forms refused by this guard. Mirrored in ADR-0008 amendment (drift guard).
WRAPPER_FORMS = (
    "sh -c",
    "bash -c",
    "zsh -c",
    "eval",
    "xargs git",
    "xargs gh",
    "env git",
    "env gh",
    "git -c",
    "git --git-dir",
    "git --exec-path",
    "gh api -X non-GET",
)

# Publishing verbs that should not be invoked by the agent.
PUBLISHING_VERBS = (
    ("git", "push"),
    ("git", "remote", "add"),
    ("git", "remote", "set-url"),
    ("gh", "pr", "create"),
    ("gh", "pr", "merge"),
    ("gh", "pr", "edit"),
    ("gh", "pr", "comment"),
    ("gh", "pr", "close"),
    ("gh", "pr", "review"),
    ("gh", "issue", "create"),
    ("gh", "issue", "edit"),
    ("gh", "issue", "comment"),
    ("gh", "issue", "close"),
    ("gh", "release", "create"),
    ("gh", "repo", "create"),
    ("gh", "repo", "delete"),
    ("gh", "gist", "create"),
    ("gh", "workflow", "run"),
)

# Maximum group nesting ($( ), ` `, ( ), { }) before the guard blocks as unparseable.
MAX_NESTING = 64

# Characters that end a word, so a following # starts a comment (quote-aware fallback trigger).
_WORD_BREAK = " \t\n;&|()`{}<>"

# Prefixes the guard looks through before checking the command word (closed set).
# Each entry is (kind, name). Applied by strip_command_prefix().
TRANSPARENT_PREFIXES = (
    ("assignment", None),     # VAR=value, repeated
    ("bare", "command"),
    ("bare", "exec"),
    ("bare", "nohup"),
    ("bare", "time"),
    ("one-positional", "timeout"),  # timeout <duration> <command>
    ("nice", "nice"),         # nice <command> or nice -n N <command>
    ("zsh-equals", None),     # =name is zsh's expansion of `which name`; applied once
)

_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_INTERPRETER_RE = re.compile(r"^(python3(\.\d+)?|python|node|perl|ruby)$")
# A short flag that takes a program: -c (python), -e (node, perl, ruby), -E (perl), -p (node).
# Combined forms such as -Ic are included, and so is a program glued to the flag (-c"...", which
# reaches the guard as one token such as -cimport os; ...).
_SHORT_EVAL_FLAG_RE = re.compile(r"^-[A-Za-z]*[ceEp]")
_PROGRAM_WORD_RE = re.compile(r"[\w./=-]+")
# String escapes that Python, Node and Perl decode: \xNN, \uNNNN, octal, and the simple ones.
_ESCAPE_RE = re.compile(r"\\(x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|[0-7]{1,3}|.)", re.DOTALL)
_SIMPLE_ESCAPES = {"n": "\n", "t": "\t", "r": "\r"}


class UnparseableCommand(Exception):
    """The splitter cannot follow the command. check_command() blocks it."""


class _NeedFallback(Exception):
    """Internal: the quote-aware pass met a construct it does not model. Use the quote-blind pass."""


def _flush(current, out):
    """Append the pending segment, stripped, to `out` and clear `current`."""
    if current:
        out.append("".join(current).strip())
        del current[:]


def _scan_single_quoted(ch, state, current):
    """Single-quoted mode: one character. Everything is literal until the closing quote.

    Returns the new state, which is None once the closing quote is consumed.
    """
    current.append(ch)
    return None if ch == "'" else state


def _scan_dollar_group(command, i, depth, out):
    """Scan the $( ) group whose `$` is at `i`. The caller has already flushed.

    Returns the index just past the group. Raises UnparseableCommand if it never closes.
    """
    end, closed = _scan(command, i + 2, depth + 1, "dollar", out)
    if not closed:
        raise UnparseableCommand("unclosed $(")
    return end


def _scan_backtick_group(command, i, depth, out):
    """Scan the ` ` group opened by the backtick at `i`. The caller has already flushed.

    Returns the index just past the group. Raises UnparseableCommand if it never closes.
    """
    end, closed = _scan(command, i + 1, depth + 1, "backtick", out)
    if not closed:
        raise UnparseableCommand("unclosed backtick")
    return end


def _scan_double_quoted(command, i, state, current):
    """Double-quoted mode: one character. Only a backslash before a newline, a double quote,
    a backslash, a dollar or a backtick changes how the next character reads.

    Returns (next index, new state).
    """
    ch = command[i]
    nxt = command[i + 1:i + 2]
    if ch == "\\" and nxt:
        if nxt == "\n":
            return i + 2, state
        if nxt in ('"', "\\", "$", "`"):
            current.append(ch)
            current.append(nxt)
            return i + 2, state
        current.append(ch)
        return i + 1, state
    if ch == '"':
        state = None
    current.append(ch)
    return i + 1, state


def _scan_unquoted_backslash(command, i, current):
    """Unquoted backslash: a line continuation is removed, otherwise the escape is kept.

    Returns the next index.
    """
    nxt = command[i + 1:i + 2]
    if nxt == "\n":
        # Line continuation: removed, so the words on either side join.
        return i + 2
    current.append("\\")
    if nxt:
        current.append(nxt)
        return i + 2
    return i + 1


def _scan_unquoted(command, i, kind, depth, current, out):
    """Unquoted mode: one character outside any quotes.

    Returns (next index, new state, closed). `closed` is True when this character closes the group.
    The new state is a quote character when a quote opens here, otherwise None.
    Raises _NeedFallback on a heredoc or a word-start #.
    """
    ch = command[i]
    nxt = command[i + 1:i + 2]

    if ch == "\\":
        return _scan_unquoted_backslash(command, i, current), None, False

    if ch in ("'", '"'):
        current.append(ch)
        return i + 1, ch, False

    if ch == "<" and nxt == "<":
        raise _NeedFallback()

    if ch == "#" and (i == 0 or command[i - 1] in _WORD_BREAK):
        raise _NeedFallback()

    if (kind in ("dollar", "paren") and ch == ")") or (kind == "brace" and ch == "}"):
        _flush(current, out)
        return i + 1, None, True

    if ch in ("(", "{"):
        _flush(current, out)
        child_kind = "paren" if ch == "(" else "brace"
        end, _closed = _scan(command, i + 1, depth + 1, child_kind, out)
        return end, None, False

    if ch in (";", "&", "|", "\n"):
        _flush(current, out)
        return i + 1, None, False

    current.append(ch)
    return i + 1, None, False


def _scan(command, pos, depth, kind, out):
    """Quote-aware scan of one group, starting at `pos`. Appends finished segments to `out`.

    `kind` is "top", "dollar" ($( ), "paren" ( ), "brace" { } or "backtick" (` `). Returns
    (index just past the group, closed). A group that never closes is inspected to the end,
    except $( and backtick, which the caller turns into an unparseable block.
    Scanning is linear: each character is visited once.

    The dispatch order is fixed: single-quoted state, then $(, then $' and $" (fallback),
    then backtick, then double-quoted state, then unquoted handling.
    """
    if depth > MAX_NESTING:
        raise UnparseableCommand("nesting too deep")

    current = []
    state = None  # None (unquoted), "'" (single-quoted) or '"' (double-quoted)
    i = pos
    n = len(command)

    while i < n:
        ch = command[i]
        nxt = command[i + 1:i + 2]

        if state == "'":
            state = _scan_single_quoted(ch, state, current)
            i += 1
            continue

        if ch == "$" and nxt == "(":
            _flush(current, out)
            i = _scan_dollar_group(command, i, depth, out)
            continue

        if ch == "$" and nxt in ("'", '"'):
            # $'...' and $"...": ANSI-C quoting can have escapes that desync quote tracking.
            # Fail closed: treat as unparseable and fall back to quote-blind split.
            raise _NeedFallback()

        if ch == "`":
            if kind == "backtick" and state is None:
                _flush(current, out)
                return i + 1, True
            _flush(current, out)
            i = _scan_backtick_group(command, i, depth, out)
            continue

        if state == '"':
            i, state = _scan_double_quoted(command, i, state, current)
            continue

        i, state, closed = _scan_unquoted(command, i, kind, depth, current, out)
        if closed:
            return i, True

    if state is not None:
        raise UnparseableCommand("unterminated quote")
    _flush(current, out)
    return n, False


def _drop_ansi_c_dollars(command):
    """Drop a $ that directly precedes a quote, unless the $ itself is escaped (odd backslash run)."""
    out = []
    backslashes = 0
    for i, ch in enumerate(command):
        nxt = command[i + 1:i + 2]
        if ch == "$" and nxt in ("'", '"') and backslashes % 2 == 0:
            backslashes = 0
            continue
        backslashes = backslashes + 1 if ch == "\\" else 0
        out.append(ch)
    return "".join(out)


def _split_quote_blind(command):
    """Quote-blind split, used when the quote-aware pass meets a heredoc or a comment.

    Splits on separators and recurses into $( ), ` `, ( ) and { } with a stack. It does not
    track quotes, so it blocks more and never allows a segment the quote-aware pass would block.
    """
    text = _drop_ansi_c_dollars(command)
    segments = []
    current = []
    stack = []

    def flush():
        if current:
            segments.append("".join(current).strip())
            del current[:]

    def push(kind):
        stack.append(kind)
        if len(stack) > MAX_NESTING:
            raise UnparseableCommand("nesting too deep")

    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        pair = text[i:i + 2]
        if pair == "\\\n":
            i += 2
            continue
        if pair == "$(":
            flush()
            push("dollar")
            i += 2
            continue
        if ch == "`":
            flush()
            if stack and stack[-1] == "backtick":
                stack.pop()
            else:
                push("backtick")
            i += 1
            continue
        if ch in ("(", "{"):
            flush()
            push("paren" if ch == "(" else "brace")
            i += 1
            continue
        if ch == ")" and stack and stack[-1] in ("dollar", "paren"):
            flush()
            stack.pop()
            i += 1
            continue
        if ch == "}" and stack and stack[-1] == "brace":
            flush()
            stack.pop()
            i += 1
            continue
        if ch in (";", "&", "|", "\n"):
            flush()
            i += 1
            continue
        current.append(ch)
        i += 1

    flush()
    if "dollar" in stack or "backtick" in stack:
        raise UnparseableCommand("unclosed $( or backtick")
    return [s for s in segments if s]


def split_segments(command):
    """Split command into simple-command segments.

    Quote-aware: separators inside '...' and "..." are literal. The contents of (...),
    {...}, $(...) and `...` are recursed into, so a publishing verb or wrapper inside a group
    is still seen. Falls back to the quote-blind split on a heredoc or a word-start #.

    Raises UnparseableCommand for an unterminated quote, an unclosed $( or backtick, or
    nesting deeper than MAX_NESTING.
    """
    out = []
    try:
        _scan(command, 0, 0, "top", out)
    except _NeedFallback:
        return _split_quote_blind(command)
    return [s for s in out if s]


def tokenize_segment(segment: str) -> Optional[List[str]]:
    """Tokenize a single shell command segment using shlex.

    Fails closed on ValueError (unparseable). Returns None on parse failure.
    """
    try:
        return shlex.split(segment)
    except ValueError:
        # Unparseable (e.g., unbalanced quotes) -> block
        return None


def _strip_one(kind, name, tokens):
    """Return tokens with one occurrence of the prefix removed, or None if it does not apply."""
    head = tokens[0]
    if kind == "assignment":
        return tokens[1:] if _ASSIGN_RE.match(head) else None
    if kind == "bare":
        if head == name:
            # Special handling for 'time' with -p option
            if name == "time" and len(tokens) >= 2 and tokens[1] == "-p":
                return tokens[2:]
            return tokens[1:]
        return None
    if kind == "one-positional":
        if head == name:
            # Special handling for 'timeout' with options
            if name == "timeout":
                # timeout -k DURATION DURATION COMMAND, timeout -s SIGNAL DURATION COMMAND
                i = 1
                while i < len(tokens) and tokens[i].startswith("-"):
                    if tokens[i] in ("-k", "-s"):
                        # These options take an argument, skip both
                        i += 2
                    else:
                        # Unknown option, fail closed
                        i += 1
                    if i >= len(tokens):
                        return None
                # Now we should have the duration and command
                if i < len(tokens) - 1 and not tokens[i].startswith("-"):
                    return tokens[i + 1:]
                return None
            # Standard one-positional: NAME DURATION COMMAND
            if len(tokens) >= 3 and not tokens[1].startswith("-"):
                return tokens[2:]
        return None
    if kind == "nice":
        if head != name:
            return None
        if len(tokens) >= 3 and tokens[1] == "-n":
            return tokens[3:]
        if len(tokens) >= 2 and not tokens[1].startswith("-"):
            return tokens[1:]
        return None
    raise ValueError(f"Unknown prefix kind: {kind}")



def strip_command_prefix(tokens):
    """Remove transparent prefixes from the front of a segment's tokens.

    Loops over TRANSPARENT_PREFIXES until none applies. Then applies the zsh =name rule once,
    but only when ("zsh-equals", None) is present in TRANSPARENT_PREFIXES (read at call time).
    Returns a new list. Only the closed set in TRANSPARENT_PREFIXES is stripped; any other
    prefix (for example `exec -a name` or `command -p`) is left in place.
    """
    tokens = list(tokens)
    changed = True
    while changed and tokens:
        changed = False
        for kind, name in TRANSPARENT_PREFIXES:
            if kind == "zsh-equals":
                continue
            stripped = _strip_one(kind, name, tokens)
            if stripped is not None:
                tokens = stripped
                changed = True
                break
    if ("zsh-equals", None) in TRANSPARENT_PREFIXES and tokens and tokens[0].startswith("=") and len(tokens[0]) > 1:
        tokens = [tokens[0][1:]] + tokens[1:]
    return tokens


def find_subcommand(tokens: List[str], tool: str) -> Optional[str]:
    """Find the subcommand (git verb or gh noun+verb) after skipping global options.

    For git: returns the verb (e.g., "push", "pr").
    For gh: returns a space-separated noun+verb (e.g., "pr merge").

    Fails closed (returns None) on unknown global options. Returns None if subcommand not found.

    Known git global options:
      -C <path>, --git-dir=..., --exec-path=..., --config-env=..., --git-common-dir=...,
      --namespace=..., --work-tree=..., --super-prefix=..., -c <name=value>, --no-pager, -p,
      --paginate, --no-replace-objects, --literal-pathspecs, --bare

    Known gh global options:
      -R <repo>, --repo <repo>, --repo=..., --json <fields>
    """
    if not tokens:
        return None

    tool_name = os.path.basename(tokens[0].strip('\'"')).lower()
    i = 1

    while i < len(tokens):
        tok = tokens[i]

        # Stop on non-option token (the subcommand)
        if not tok.startswith("-"):
            break

        # Git options that take a separate value token
        if tok in ("-C", "--git-dir", "--exec-path", "--config-env", "--git-common-dir",
                   "--namespace", "--work-tree", "--super-prefix", "-c"):
            # Skip the option and its value
            i += 2
            continue

        # Git options with = form
        if tok.startswith(("--git-dir=", "--exec-path=", "--config-env=", "--git-common-dir=",
                          "--namespace=", "--work-tree=", "--super-prefix=")):
            i += 1
            continue

        # Git flags without value
        if tok in ("--no-pager", "-p", "--paginate", "--no-replace-objects", "--literal-pathspecs", "--bare"):
            i += 1
            continue

        # gh options with value
        if tok in ("-R", "--repo", "--json"):
            i += 2
            continue

        # gh options with = form
        if tok.startswith(("--repo=", "--json=")):
            i += 1
            continue

        # Unknown option: fail closed
        return None

    # Found the subcommand
    if i < len(tokens):
        if tool_name == "git":
            return tokens[i].lower()
        elif tool_name == "gh":
            # For gh, the subcommand is noun verb (e.g., "pr merge")
            if i + 1 < len(tokens):
                return f"{tokens[i].lower()} {tokens[i + 1].lower()}"
            else:
                return tokens[i].lower()

    return None


def skip_global_options(tokens: List[str], start: int = 0) -> int:
    """Skip git/gh global options and return the index of the first non-option token.

    Handles known git/gh global options. For unknown global options, uses fail-closed
    approach: scans remaining tokens to find the verb regardless, or blocks if too much
    ambiguity.

    Handles:
      - git: -C <dir>, --git-dir=, --exec-path=, --config-env=, --git-common-dir=,
             --namespace=, --work-tree=, -c <name=value>, and other -X= or -X value forms
      - gh: -R <repo>, --repo <repo>, --json <fields>

    For git, skips -C and its argument but does NOT skip -c (which is in the deny list).
    """
    i = start
    while i < len(tokens):
        tok = tokens[i]

        # git -C <dir>
        if tok == "-C" and i + 1 < len(tokens):
            i += 2
            continue

        # git global options with = form (--git-dir=, --exec-path=, --config-env=, etc.)
        if tok.startswith("--git-dir=") or tok.startswith("--exec-path=") or \
           tok.startswith("--config-env=") or tok.startswith("--git-common-dir=") or \
           tok.startswith("--namespace=") or tok.startswith("--work-tree="):
            i += 1
            continue

        # gh -R <repo> or gh --repo <repo> or gh --json <fields>
        if (tok == "-R" or tok == "--repo" or tok == "--json") and i + 1 < len(tokens):
            i += 2
            continue

        # gh --repo=... or gh --json=...
        if tok.startswith("--repo=") or tok.startswith("--json="):
            i += 1
            continue

        # git global options that take a separate value token
        if tok in ("--git-dir", "--work-tree", "--namespace", "--exec-path",
                   "--config-env", "--super-prefix", "--git-common-dir", "-c") and i + 1 < len(tokens):
            i += 2
            continue

        # Any other option (e.g. --no-pager, -p, --bare, --paginate): consume ONLY this
        # token. Never swallow the next token, which may be the verb (fail-closed).
        if tok.startswith("-"):
            i += 1
            continue

        # Not an option; stop here
        break

    return i


def is_wrapper_form(tokens: List[str]) -> bool:
    """Check if tokens match a forbidden wrapper form.

    Returns True if any WRAPPER_FORMS pattern is detected.
    """
    if not tokens:
        return False

    # Check each wrapper form
    for wrapper in WRAPPER_FORMS:
        parts = wrapper.split()

        # Special handling for wrappers that have variable intermediate args
        if wrapper == "env git":
            # Match: env <any args> git <any args>
            for i in range(len(tokens)):
                if tokens[i].lower() == "env":
                    for j in range(i + 1, len(tokens)):
                        if tokens[j].lower() == "git":
                            return True
            continue

        if wrapper == "env gh":
            # Match: env <any args> gh <any args>
            for i in range(len(tokens)):
                if tokens[i].lower() == "env":
                    for j in range(i + 1, len(tokens)):
                        if tokens[j].lower() == "gh":
                            return True
            continue

        if wrapper == "gh api -X non-GET":
            # Match: gh api with -X or --method set to anything other than GET
            for i in range(len(tokens)):
                if tokens[i].lower() == "gh" and i + 1 < len(tokens) and tokens[i + 1].lower() == "api":
                    # Look for -X or --method flags after "gh api"
                    j = i + 2
                    while j < len(tokens):
                        tok = tokens[j]
                        if tok == "-X":
                            if j + 1 < len(tokens):
                                method = tokens[j + 1].upper()
                                if method != "GET":
                                    return True
                            break
                        elif tok.startswith("-X"):
                            method = tok[2:].upper()
                            if method != "GET":
                                return True
                            break
                        elif tok == "--method":
                            if j + 1 < len(tokens):
                                method = tokens[j + 1].upper()
                                if method != "GET":
                                    return True
                            break
                        elif tok.startswith("--method="):
                            method = tok.split("=", 1)[1].upper()
                            if method != "GET":
                                return True
                            break
                        elif tok.startswith("-"):
                            j += 1
                        else:
                            break
                        j += 1
            continue

        # Standard exact-sequence matching for other wrappers
        for i in range(len(tokens)):
            if tokens[i].lower() == parts[0]:
                # Try to match the rest of the wrapper
                match = True
                for j, part in enumerate(parts[1:], 1):
                    if i + j >= len(tokens):
                        match = False
                        break
                    # For "git -c" we need special handling to match --git-dir= etc
                    if parts[0] == "git" and part in ("-c", "--git-dir", "--exec-path"):
                        # These can appear as combined forms (--git-dir=value)
                        # but only match the exact option, not -C (which is a global option)
                        tok = tokens[i + j]  # Don't lowercase this one
                        if part == "-c" and tok == "-c":
                            # Exact match for -c (case-sensitive to exclude -C)
                            match = True
                            break
                        elif part == "--git-dir" and tok.startswith("--git-dir"):
                            # Match --git-dir= forms
                            match = True
                            break
                        elif part == "--exec-path" and tok.startswith("--exec-path"):
                            # Match --exec-path= forms
                            match = True
                            break
                        else:
                            match = False
                            break
                    if tokens[i + j].lower() != part:
                        match = False
                        break

                if match:
                    return True

    return False


def is_publishing_verb(tokens: List[str]) -> bool:
    """Check if tokens match a forbidden publishing verb (multi-word tuple).

    Skips global options before checking. Returns True if forbidden verb is detected.
    """
    if not tokens:
        return False

    # Get the first token (likely git or gh)
    cmd_basename = os.path.basename(tokens[0].strip('\'"')).lower()

    if cmd_basename == "git":
        # Skip git's global options
        verb_start = skip_global_options(tokens, 1)

        # Now check if remaining tokens match any git verb tuple
        if verb_start < len(tokens):
            verb_tokens = tokens[verb_start:]

            for pub_verb in PUBLISHING_VERBS:
                if pub_verb[0] != "git":
                    continue

                # Match the verb tuple (git, subcommand, ...)
                verb_parts = pub_verb[1:]  # Skip "git"

                if len(verb_tokens) >= len(verb_parts):
                    match = True
                    for j, part in enumerate(verb_parts):
                        if verb_tokens[j].lower() != part:
                            match = False
                            break

                    if match:
                        return True

    elif cmd_basename == "gh":
        # Skip gh's global options
        verb_start = skip_global_options(tokens, 1)

        # Now check if remaining tokens match any gh verb tuple
        if verb_start < len(tokens):
            verb_tokens = tokens[verb_start:]

            for pub_verb in PUBLISHING_VERBS:
                if pub_verb[0] != "gh":
                    continue

                # Match the verb tuple (gh, subcommand, verb, ...)
                verb_parts = pub_verb[1:]  # Skip "gh"

                if len(verb_tokens) >= len(verb_parts):
                    match = True
                    for j, part in enumerate(verb_parts):
                        if verb_tokens[j].lower() != part:
                            match = False
                            break

                    if match:
                        return True

    return False


def _program_words(tokens):
    """Split the tokens of an interpreter program into bare words."""
    words = []
    for tok in tokens:
        words.extend(_PROGRAM_WORD_RE.findall(tok))
    return words


def _decode_program_escapes(text):
    """Decode the string escapes a program may spell a verb with (git\\x20push, git\\040push, git\\npush).

    An unknown escape keeps its character and drops the backslash.
    """
    def repl(match):
        body = match.group(1)
        if body[0] in "xu" and len(body) > 1:
            return chr(int(body[1:], 16))
        if body[0] in "01234567":
            return chr(int(body, 8))
        return _SIMPLE_ESCAPES.get(body, body)

    return _ESCAPE_RE.sub(repl, text)


def _drop_git_config(words):
    """Drop git's `-c <key=value>` pair (and a glued `-c<key=value>`) from program words.

    Interpreter path only: git's -c takes a value, so without this the subcommand after it is not seen.
    """
    kept = []
    skip_value = False
    for i, word in enumerate(words):
        if skip_value:
            skip_value = False
            continue
        if word == "-c" and i + 1 < len(words):
            skip_value = True
            continue
        if word.startswith("-c") and len(word) > 2:
            continue
        kept.append(word)
    return kept


def is_interpreter_verb_pair(tokens):
    """Check whether a python/node/perl/ruby -c/-e program contains a git or gh verb pair.

    The program text is split into words, both as written and with string escapes decoded. At
    each word that is git or gh, the words that follow (with git -c pairs removed) are checked
    with is_publishing_verb(). Only programs after an eval flag are checked; a script file is not read.
    Stops scanning for eval flags once a positional argument (non-option) is encountered.
    """
    if not tokens:
        return False
    name = os.path.basename(tokens[0]).lower()
    if not _INTERPRETER_RE.match(name):
        return False

    for k in range(1, len(tokens)):
        tok = tokens[k]
        # Stop at the first positional argument (script file or argument)
        if not tok.startswith("-"):
            break
        is_eval_flag = (
            _SHORT_EVAL_FLAG_RE.match(tok) is not None
            or tok in ("--eval", "--print")
            or tok.startswith("--eval=")
        )
        if not is_eval_flag:
            continue
        tail = tokens[k:]
        for variant in (tail, [_decode_program_escapes(part) for part in tail]):
            words = _program_words(variant)
            for j, word in enumerate(words):
                if word.lower() in ("git", "gh") and is_publishing_verb(
                    [word] + _drop_git_config(words[j + 1:])
                ):
                    return True
    return False


def check_command(command: str) -> Tuple[bool, str]:
    """flow-implement check as (is_blocked, reason); a thin wrapper kept for in-process tests.

    The hook itself goes through check_command_for_agent().
    """
    blocked, reason, _rule = check_command_for_agent(command, "flow-implement")
    return blocked, reason


def check_push_form(tokens: List[str]) -> Tuple[bool, str]:
    """Check if tokens match an allowed git push form.

    Allowed forms (flow-bash-push-allowlist:begin/end marked block):
    - git push [-u] origin <branch>  (exactly one branch arg, no special chars)
    - git push --force-with-lease --force-if-includes origin HEAD  (exact flags)
    - git -C <wt> push --force-with-lease  (with optional -C option)
    - git push --force-with-lease=<branch>:<40-hex> origin <40-hex>:refs/heads/<branch>
      (exact form with 40-hex SHAs and matching branch names)

    Denied: any --force (bare or -f), --mirror, --delete, +refspec, :branch deletion,
    extra flags, extra positionals, short SHAs, or any other variant.
    Known wrapper bypasses (python3 -c, eval) are separate checks.

    Returns: (is_allowed: bool, reason: str if denied)
    """
    if not tokens or tokens[0].lower() != "git":
        return False, ""

    # Find the push subcommand using the helper
    if find_subcommand(tokens, "git") != "push":
        return False, ""

    # Skip past tool and global options to find push using the shared helper
    i = skip_global_options(tokens, 1)

    # Now we should be at "push"
    if i >= len(tokens) or tokens[i].lower() != "push":
        return False, ""

    i += 1  # Move past "push"
    push_tokens = tokens[i:]

    if not push_tokens:
        return False, "git push with no arguments"

    # Form 1: git push [-u] origin <branch>
    # Must have: optional -u/--set-upstream, then "origin", then exactly one branch
    # with no -, +, or : in it
    optional_flag_count = 0
    filtered = []
    for t in push_tokens:
        if t in ("-u", "--set-upstream"):
            optional_flag_count += 1
        else:
            filtered.append(t)

    if len(filtered) == 2 and filtered[0].lower() == "origin":
        branch = filtered[1]
        # Reject branches with -, +, or : (invalid branch names)
        if not (branch.startswith("-") or branch.startswith("+") or ":" in branch):
            return True, ""

    # Form 2: git push --force-with-lease --force-if-includes origin HEAD
    # Must be EXACTLY these flags and EXACTLY origin HEAD (no other tokens)
    flags = [t for t in push_tokens if t.startswith("-")]
    non_flags = [t for t in push_tokens if not t.startswith("-")]

    # Check for exact flag set
    if sorted(flags) == ["--force-if-includes", "--force-with-lease"]:
        # Must have exactly origin HEAD and nothing else
        if len(non_flags) == 2 and non_flags[0].lower() == "origin" and non_flags[1].upper() == "HEAD":
            return True, ""

    # Form 3: git -C <wt> push --force-with-lease (only flag, no other args)
    if flags == ["--force-with-lease"] and len(non_flags) == 0:
        return True, ""

    # Form 4: git push --force-with-lease=<branch>:<40-hex> origin <40-hex>:refs/heads/<branch>
    # Must be EXACTLY one --force-with-lease=<branch>:<40hex> token, then origin, then refspec
    # with matching branch names
    sha_40_pattern = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)

    for token in push_tokens:
        if token.startswith("--force-with-lease="):
            lease_part = token[len("--force-with-lease="):]
            if ":" not in lease_part:
                continue
            parts = lease_part.split(":")
            if len(parts) != 2:
                continue
            lease_branch, lease_sha = parts
            # Verify lease_sha is exactly 40 hex
            if not sha_40_pattern.match(lease_sha):
                continue

            # Collect all other flags (should be none)
            other_flags = [t for t in push_tokens if t.startswith("-") and t != token]
            if other_flags:
                continue

            # Collect non-flag tokens
            non_flag_tokens = [t for t in push_tokens if not t.startswith("-")]
            if len(non_flag_tokens) != 2:
                continue

            remote, refspec = non_flag_tokens
            if remote.lower() != "origin":
                continue

            # Parse refspec: must be <40-hex>:refs/heads/<branch>
            if ":" not in refspec:
                continue
            refspec_parts = refspec.split(":", 1)
            if len(refspec_parts) != 2:
                continue
            refspec_sha, refspec_ref = refspec_parts

            # Verify sha is 40 hex
            if not sha_40_pattern.match(refspec_sha):
                continue

            # Verify refspec matches: refs/heads/<branch> where <branch> == lease_branch
            if not refspec_ref.startswith("refs/heads/"):
                continue
            refspec_branch = refspec_ref[len("refs/heads/"):]

            # Both branches must match
            if lease_branch == refspec_branch:
                return True, ""

    return False, "unknown or disallowed git push form"


def check_command_for_agent(command: str, agent: str) -> Tuple[bool, str, str]:
    """Check command against agent-specific rules.

    Per-agent rules:
    - flow-implement: full checks (wrappers and publishing verbs)
    - flow-review: pass through (allow all)
    - flow-healer: pass through (its tools list already restricts Bash to scoped git)
    - flow-ship: restricted wrappers, allowed git push forms, all gh pr merge denied
    - flow-merge: restricted wrappers, allowed git push forms, gh pr merge --admin denied,
      gh pr merge --squash allowed only if no merge-queue config exists

    Returns: (is_blocked: bool, reason: str if blocked, rule_id: str)
    """
    if agent in ("flow-review", "flow-healer"):
        # flow-review: pass through. flow-healer: pass through too; its Bash is already limited
        # by its tools list to git diff/status/rev-parse/add/commit/checkout HEAD -- (no push,
        # no gh), so this guard adds nothing (#380; ADR-0023 2026-10-29 amendment).
        return False, "", ""

    # Fail closed on unknown flow-* agents; allow non-flow agents (e.g., general-purpose)
    if agent.startswith("flow-") and agent not in KNOWN_FLOW_AGENTS:
        return True, f"unknown flow agent: {agent}", "unknown-agent"

    if agent not in KNOWN_FLOW_AGENTS:
        return False, "", ""

    if not command or not isinstance(command, str):
        return True, "Empty or invalid command", "invalid-input"

    if agent in ("flow-ship", "flow-merge") and check_cmd_assignment_denied(command):
        return True, "CHECK_CMD may only be assigned from the cached check command", "wrapper-form"

    try:
        segments = split_segments(command)
    except UnparseableCommand as exc:
        return True, f"Unparseable command: {exc}", "parse-error"
    cwd = None
    try:
        cwd = os.getcwd()
    except Exception:
        pass

    for segment in segments:
        if not segment:
            continue

        tokens = tokenize_segment(segment)
        if tokens is None:
            # Parse error (e.g., unbalanced quotes) -> block
            return True, f"Unparseable command: {segment}", "parse-error"

        # Transparent prefixes (command, exec, nohup, time, timeout, nice, VAR=value) are
        # stripped so the rules below see the real command.
        tokens = strip_command_prefix(tokens)

        if agent == "flow-implement":
            # flow-implement: full checks
            if is_wrapper_form(tokens):
                return True, "shell wrappers (sh -c, bash -c, eval, etc.) are not allowed", "wrapper-form"

            if is_publishing_verb(tokens):
                return True, "publishing commands (git push, gh pr create, etc.) are not allowed", "publishing-verb"

            if is_interpreter_verb_pair(tokens):
                return True, "interpreter -c/-e program runs a publishing command (git push, gh pr create, etc.)", "publishing-verb"

        elif agent in ("flow-ship", "flow-merge"):
            # flow-ship and flow-merge: restricted rules

            # Check for wrapper forms, but allow specific python3 -c and eval forms
            if is_wrapper_form_restricted(tokens, agent):
                return True, "restricted wrapper form not allowed", "wrapper-form"

            # For git/gh commands, fail closed if subcommand cannot be determined (unknown option)
            cmd_basename = os.path.basename(tokens[0].strip('\'"')).lower() if tokens else ""
            if cmd_basename in ("git", "gh"):
                tool = "git" if cmd_basename == "git" else "gh"
                subcommand = find_subcommand(tokens, tool)
                # If subcommand is None, it means an unknown option was encountered: fail closed
                if subcommand is None:
                    msg = f"Unable to parse {cmd_basename} command (unknown option). {ESCALATION_HINT}"
                    return True, msg, "unknown-option"

            # Check git push specifically using the shared helper
            if find_subcommand(tokens, "git") == "push":
                is_allowed, reason = check_push_form(tokens)
                if not is_allowed:
                    msg = f"{reason} {ESCALATION_HINT}"
                    return True, msg, "push-form"

            # Check gh pr merge rules
            if find_subcommand(tokens, "gh") == "pr merge":
                # flow-ship: deny ALL gh pr merge (any flags)
                if agent == "flow-ship":
                    msg = f"gh pr merge is not allowed for flow-ship. {ESCALATION_HINT}"
                    return True, msg, "gh-pr-merge-ship"

                # flow-merge: deny --admin, check merge-queue for --squash
                if agent == "flow-merge":
                    # Check for --admin flag (denied for flow-merge)
                    if "--admin" in tokens:
                        msg = f"gh pr merge --admin is not allowed for flow-merge. {ESCALATION_HINT}"
                        return True, msg, "gh-pr-merge-admin"

                    # Check for --squash flag: only allowed if no merge-queue config
                    if "--squash" in tokens:
                        if has_merge_queue_config(cwd):
                            msg = f"gh pr merge --squash is not allowed when a merge-queue config exists. {ESCALATION_HINT}"
                            return True, msg, "gh-pr-merge-queue"
                        # If no merge-queue, --squash is allowed

            # gh stack sync is allowed
            # gh pr create/edit are not blocked

    return False, "", ""


def _is_allowed_eval(tokens: List[str]) -> bool:
    """True only for the exact eval literals in the Skill closure.

    - eval "$CHECK_CMD"                  (worktree-reference.md Restack-a-child gate)
    - eval ls -d "${PATTERN}/" [2>/dev/null]   (cleanup.md glob expansion)
    Anything else, including other variables or extra arguments, is denied.
    """
    if tokens == ["eval", "$CHECK_CMD"]:
        return True
    if tokens[:4] == ["eval", "ls", "-d", "${PATTERN}/"]:
        return all(t.startswith(("2>", ">")) for t in tokens[4:])
    return False


# CHECK_CMD may only be assigned from the project's cached check command. An assignment from
# anything else, followed by the allowed `eval "$CHECK_CMD"`, would run arbitrary text.
# Acceptable forms:
#   $(jq -r '.commands.check // .commands.test // empty' <file>)
#   $(jq -r '.commands.check // .commands.test // empty' <file> 2>/dev/null)
#   $(jq -r '.command // empty' <file>)
# where <file> is a variable placeholder or quoted path
_CHECK_CMD_ASSIGN_RE = re.compile(r"(?:^|[\s;&|(])CHECK_CMD=(.*)$", re.MULTILINE)
_CHECK_CMD_OK_PATTERN = re.compile(
    r"^\$\(jq\s+-r\s+'\.commands\.check // .commands\.test // empty'\s+(?:'[^']*'|[^'\s)]+|<[^>]+>)\s*(?:2>/dev/null)?\)$"
    r"|^\$\(jq\s+-r\s+'\.command // empty'\s+(?:'[^']*'|[^'\s)]+|<[^>]+>)\s*(?:2>/dev/null)?\)$"
)


def check_cmd_assignment_denied(command: str) -> bool:
    """True when the command assigns CHECK_CMD from anything but the cached check command."""
    if "eval" not in command:
        return False
    for line in command.splitlines():
        for m in _CHECK_CMD_ASSIGN_RE.finditer(line):
            assigned_value = m.group(1).strip()
            if not _CHECK_CMD_OK_PATTERN.match(assigned_value):
                return True
    return False


def has_merge_queue_config(cwd: Optional[str] = None) -> bool:
    """Check if a merge-queue.json config exists for the repo.

    Uses the same lookup logic as scripts/workflow/merge_queue.py:resolve_config_path().
    Returns True if config exists, False if not found or if resolution fails.
    """
    if cwd is None:
        cwd = os.getcwd()

    try:
        cwd_path = Path(cwd).resolve()

        # Try to determine git common dir
        try:
            result = subprocess.run(
                ["git", "-C", str(cwd_path), "rev-parse", "--git-common-dir"],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode != 0:
                return False
            git_common_dir_str = result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

        # Resolve the git common dir path
        if not Path(git_common_dir_str).is_absolute():
            git_common_dir = (cwd_path / git_common_dir_str).resolve()
        else:
            git_common_dir = Path(git_common_dir_str).resolve()

        # Check for .bare layout (bare-repo + worktrees)
        if git_common_dir.name == ".bare" and (git_common_dir.parent / "worktrees").is_dir():
            config_path = git_common_dir.parent / "merge-queue.json"
            return config_path.exists()

        # Check for /setup-repo layout (worktrees/main/.git)
        if git_common_dir.parent.parent.name == "worktrees":
            container = git_common_dir.parent.parent.parent
            config_path = container / "merge-queue.json"
            return config_path.exists()

        # Unknown layout
        return False

    except Exception:
        # On any error, conservatively return False (no queue found)
        return False


def is_wrapper_form_restricted(tokens: List[str], agent: str) -> bool:
    """Check if tokens match a forbidden wrapper form for flow-ship/flow-merge agents.

    Allows any python3 -c form (known bypass per ADR-0008) and specific eval literals.
    Denies:
    - sh -c, bash -c, zsh -c
    - xargs git, xargs gh
    - env git, env gh
    - git -c, git --git-dir, git --exec-path
    - gh api -X non-GET

    Allows (exact eval matches):
    - eval ls -d "${PATTERN}/" (cleanup.md)
    - eval "$CHECK_CMD" (worktree-reference.md)

    Also denies variable-assignment forms that expand allowed eval.
    """
    if not tokens:
        return False

    # Check for python3 -c forms (allowed)
    if len(tokens) >= 2 and tokens[0] == "python3" and tokens[1] == "-c":
        # These forms are allowed, don't block
        return False

    # Check for eval forms: only the two exact literals the closure uses are allowed
    if tokens[0] == "eval":
        return not _is_allowed_eval(tokens)

    # Check for standard denied wrapper forms (using WRAPPER_FORMS list)
    for wrapper in WRAPPER_FORMS:
        parts = wrapper.split()

        # Special handling for env git/gh and xargs git/gh
        if wrapper == "env git":
            for i in range(len(tokens)):
                if tokens[i].lower() == "env":
                    for j in range(i + 1, len(tokens)):
                        if tokens[j].lower() == "git":
                            return True  # Denied
            continue

        if wrapper == "env gh":
            for i in range(len(tokens)):
                if tokens[i].lower() == "env":
                    for j in range(i + 1, len(tokens)):
                        if tokens[j].lower() == "gh":
                            return True  # Denied
            continue

        if wrapper == "xargs git":
            for i in range(len(tokens)):
                if tokens[i].lower() == "xargs":
                    for j in range(i + 1, len(tokens)):
                        if tokens[j].lower() == "git":
                            return True  # Denied
            continue

        if wrapper == "xargs gh":
            for i in range(len(tokens)):
                if tokens[i].lower() == "xargs":
                    for j in range(i + 1, len(tokens)):
                        if tokens[j].lower() == "gh":
                            return True  # Denied
            continue

        # gh api -X non-GET check
        if wrapper == "gh api -X non-GET":
            for i in range(len(tokens)):
                if tokens[i].lower() == "gh" and i + 1 < len(tokens) and tokens[i + 1].lower() == "api":
                    j = i + 2
                    while j < len(tokens):
                        tok = tokens[j]
                        if tok == "-X":
                            if j + 1 < len(tokens):
                                method = tokens[j + 1].upper()
                                if method != "GET":
                                    return True  # Denied
                            break
                        elif tok.startswith("-X"):
                            method = tok[2:].upper()
                            if method != "GET":
                                return True  # Denied
                            break
                        elif tok.startswith("-"):
                            j += 1
                        else:
                            break
                        j += 1
            continue

        # Standard exact-sequence matching for other wrappers
        for i in range(len(tokens)):
            if tokens[i].lower() == parts[0]:
                match = True
                for j, part in enumerate(parts[1:], 1):
                    if i + j >= len(tokens):
                        match = False
                        break
                    # Special handling for git -c (not -C which is a global option)
                    if parts[0] == "git" and part == "-c":
                        tok = tokens[i + j]
                        if tok == "-c":
                            match = True
                            break
                        else:
                            match = False
                            break
                    if tokens[i + j].lower() != part:
                        match = False
                        break

                if match:
                    return True  # Denied

    return False


# flow-bash-push-allowlist:begin
# Git push forms allowed for flow-ship and flow-merge agents.
# All other git push forms are denied. Forms matched on parsed argv, never substrings.
#
# Allowed forms:
# - git push [-u] origin <branch>
# - git push --force-with-lease --force-if-includes origin HEAD
# - git -C <wt> push --force-with-lease ...
# - git push --force-with-lease=<branch>:<40-hex> origin <40-hex>:refs/heads/<branch>
# - gh stack sync
#
# Denied forms include: bare --force, -f, combined short flags with f, abbreviated
# long options (--forc), +refspec, :branch deletion, --mirror, --delete/-d, --prune.
# flow-bash-push-allowlist:end


def main():
    """
    Main entry point. Exit codes:
      - 0: allow the tool call
      - 2: block the tool call
    """
    try:
        # Parse CLI argument
        if len(sys.argv) < 3 or sys.argv[1] != "--agent":
            print("Usage: flow-bash-guard.py --agent <flow-implement|flow-review|flow-ship|flow-merge|flow-healer|auto>", file=sys.stderr)
            sys.exit(2)

        agent = sys.argv[2]
        if agent not in ("flow-implement", "flow-review", "flow-ship", "flow-merge", "auto"):
            print(f"Unknown --agent value: {agent}", file=sys.stderr)
            sys.exit(2)

        # Parse JSON payload from stdin
        payload = json.load(sys.stdin)

        # Validate payload structure
        if not isinstance(payload, dict):
            print("Invalid payload: not a dict", file=sys.stderr)
            sys.exit(2)

        # Auto mode: check agent_type in payload
        if agent == "auto":
            agent_type = payload.get("agent_type")

            # If agent_type is missing or not flow-*, allow
            if not agent_type or not agent_type.startswith("flow-"):
                print(f"Auto mode: agent_type '{agent_type}' is not a flow agent, allowing", file=sys.stderr)
                sys.exit(0)

            # If agent_type starts with flow- but not in known set, fail closed
            if agent_type not in ("flow-implement", "flow-review", "flow-ship", "flow-merge", "flow-healer"):
                print(f"Auto mode: agent_type '{agent_type}' is unknown, denying", file=sys.stderr)
                sys.exit(2)

            # Use the agent_type from payload
            agent = agent_type
        else:
            # Explicit agent specified; if it's flow-* and unknown, fail closed
            if agent.startswith("flow-") and agent not in ("flow-implement", "flow-review", "flow-ship", "flow-merge", "flow-healer"):
                print(f"Explicit agent '{agent}' is unknown, denying", file=sys.stderr)
                sys.exit(2)

        # flow-review and flow-healer have no Bash restrictions here; pass through before payload checks
        if agent in ("flow-review", "flow-healer"):
            sys.exit(0)

        tool_name = payload.get("tool_name")
        if tool_name is None:
            print("Missing tool_name in payload", file=sys.stderr)
            sys.exit(2)

        if tool_name != "Bash":
            sys.exit(0)  # Not a Bash call; allow

        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            print("Invalid tool_input: not a dict", file=sys.stderr)
            sys.exit(2)

        command = tool_input.get("command")
        if not isinstance(command, str) or not command:
            print("Invalid command: not a non-empty string", file=sys.stderr)
            sys.exit(2)

        # Check command against agent-specific rules
        is_blocked, reason, rule_id = check_command_for_agent(command, agent)
        if is_blocked:
            if rule_id and rule_id in ("push-form", "gh-pr-merge-admin", "gh-pr-merge-ship", "gh-pr-merge-queue"):
                # Denied push/merge forms: print with agent and rule id for Ask block escalation
                print(f"{agent}: rule {rule_id}: {reason}", file=sys.stderr)
            else:
                print(f"BLOCKED: {reason}", file=sys.stderr)
            sys.exit(2)

        sys.exit(0)

    except json.JSONDecodeError:
        print("Failed to parse JSON payload", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Unexpected error in hook: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
