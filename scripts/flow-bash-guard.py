#!/usr/bin/env python3
"""
PreToolUse hook for flow-implement agent: best-effort Bash command guard.

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

Exit codes:
  - 0: allow (command is safe)
  - 2: block (command violates a rule or is unparseable)
"""

import json
import os
import re
import shlex
import sys

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
# Combined forms such as -Ic are included.
_SHORT_EVAL_FLAG_RE = re.compile(r"^-[A-Za-z]*[ceEp]$")
_PROGRAM_WORD_RE = re.compile(r"[\w./=-]+")


class UnparseableCommand(Exception):
    """The splitter cannot follow the command. check_command() blocks it."""


class _NeedFallback(Exception):
    """Internal: the quote-aware pass met a construct it does not model. Use the quote-blind pass."""


def _scan(command, pos, depth, kind, out):
    """Quote-aware scan of one group, starting at `pos`. Appends finished segments to `out`.

    `kind` is "top", "dollar" ($( ), "paren" ( ), "brace" { } or "backtick" (` `). Returns
    (index just past the group, closed). A group that never closes is inspected to the end,
    except $( and backtick, which the caller turns into an unparseable block.
    Scanning is linear: each character is visited once.
    """
    if depth > MAX_NESTING:
        raise UnparseableCommand("nesting too deep")

    current = []
    state = None  # None (unquoted), "'" (single-quoted) or '"' (double-quoted)
    i = pos
    n = len(command)

    def flush():
        if current:
            out.append("".join(current).strip())
            del current[:]

    while i < n:
        ch = command[i]
        nxt = command[i + 1:i + 2]

        if state == "'":
            # Single quotes: everything is literal until the closing quote.
            current.append(ch)
            if ch == "'":
                state = None
            i += 1
            continue

        if ch == "$" and nxt == "(":
            flush()
            end, closed = _scan(command, i + 2, depth + 1, "dollar", out)
            if not closed:
                raise UnparseableCommand("unclosed $(")
            i = end
            continue

        if ch == "$" and nxt in ("'", '"'):
            # $'...' and $"...": the $ is dropped so the quoted text is read as the word it is.
            i += 1
            continue

        if ch == "`":
            if kind == "backtick" and state is None:
                flush()
                return i + 1, True
            flush()
            end, closed = _scan(command, i + 1, depth + 1, "backtick", out)
            if not closed:
                raise UnparseableCommand("unclosed backtick")
            i = end
            continue

        if state == '"':
            if ch == "\\" and nxt:
                if nxt == "\n":
                    i += 2
                elif nxt in ('"', "\\", "$", "`"):
                    current.append(ch)
                    current.append(nxt)
                    i += 2
                else:
                    current.append(ch)
                    i += 1
                continue
            if ch == '"':
                state = None
            current.append(ch)
            i += 1
            continue

        # Unquoted from here on.
        if ch == "\\":
            if nxt == "\n":
                # Line continuation: removed, so the words on either side join.
                i += 2
                continue
            current.append(ch)
            if nxt:
                current.append(nxt)
                i += 2
            else:
                i += 1
            continue

        if ch in ("'", '"'):
            state = ch
            current.append(ch)
            i += 1
            continue

        if ch == "<" and nxt == "<":
            raise _NeedFallback()

        if ch == "#" and (i == 0 or command[i - 1] in _WORD_BREAK):
            raise _NeedFallback()

        if (kind in ("dollar", "paren") and ch == ")") or (kind == "brace" and ch == "}"):
            flush()
            return i + 1, True

        if ch in ("(", "{"):
            flush()
            child_kind = "paren" if ch == "(" else "brace"
            end, _closed = _scan(command, i + 1, depth + 1, child_kind, out)
            i = end
            continue

        if ch in (";", "&", "|", "\n"):
            flush()
            i += 1
            continue

        current.append(ch)
        i += 1

    if state is not None:
        raise UnparseableCommand("unterminated quote")
    flush()
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


def tokenize_segment(segment):
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
        return tokens[1:] if head == name else None
    if kind == "one-positional":
        if head == name and len(tokens) >= 3 and not tokens[1].startswith("-"):
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
    return None


def strip_command_prefix(tokens):
    """Remove transparent prefixes from the front of a segment's tokens.

    Loops over TRANSPARENT_PREFIXES until none applies. Then applies the zsh =name rule once.
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
    if tokens and tokens[0].startswith("=") and len(tokens[0]) > 1:
        tokens = [tokens[0][1:]] + tokens[1:]
    return tokens


def skip_global_options(tokens, start=0):
    """Skip git/gh global options and return the index of the first non-option token.

    Handles known git/gh global options. For unknown global options, uses fail-closed
    approach: scans remaining tokens to find the verb regardless, or blocks if too much
    ambiguity.

    Handles:
      - git: -C <dir>, --git-dir=, --exec-path=, --config-env=, --git-common-dir=,
             --namespace=, --work-tree=, and other -X= or -X value forms
      - gh: -R <repo>, --repo <repo>

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

        # gh -R <repo> or gh --repo <repo>
        if (tok == "-R" or tok == "--repo") and i + 1 < len(tokens):
            i += 2
            continue

        # git global options that take a separate value token
        if tok in ("--git-dir", "--work-tree", "--namespace", "--exec-path",
                   "--config-env", "--super-prefix", "--git-common-dir") and i + 1 < len(tokens):
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


def is_wrapper_form(tokens):
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


def is_publishing_verb(tokens):
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


def is_interpreter_verb_pair(tokens):
    """Check whether a python/node/perl/ruby -c/-e program contains a git or gh verb pair.

    The program text is split into words. At each word that is git or gh, the words that
    follow are checked with is_publishing_verb(). Only programs after an eval flag are
    checked; a script file is not read.
    """
    if not tokens:
        return False
    name = os.path.basename(tokens[0]).lower()
    if not _INTERPRETER_RE.match(name):
        return False

    for k in range(1, len(tokens)):
        tok = tokens[k]
        is_eval_flag = (
            _SHORT_EVAL_FLAG_RE.match(tok) is not None
            or tok in ("--eval", "--print")
            or tok.startswith("--eval=")
        )
        if not is_eval_flag:
            continue
        words = _program_words(tokens[k:])
        for j, word in enumerate(words):
            if word.lower() in ("git", "gh") and is_publishing_verb([word] + words[j + 1:]):
                return True
    return False


def check_command(command):
    """Check if a command violates wrapper or verb rules.

    Returns: (is_blocked: bool, reason: str)
    - (True, reason): command is blocked
    - (False, ""): command is allowed
    """
    if not command or not isinstance(command, str):
        return True, "Empty or invalid command"

    try:
        segments = split_segments(command)
    except UnparseableCommand as exc:
        return True, f"unparseable: {exc}"

    for segment in segments:
        if not segment:
            continue

        tokens = tokenize_segment(segment)
        if tokens is None:
            # Parse error (e.g., unbalanced quotes) -> block
            return True, f"Unparseable command: {segment}"

        tokens = strip_command_prefix(tokens)

        if is_wrapper_form(tokens):
            return True, "shell wrappers (sh -c, bash -c, eval, etc.) are not allowed"

        if is_publishing_verb(tokens):
            return True, "publishing commands (git push, gh pr create, etc.) are not allowed"

        if is_interpreter_verb_pair(tokens):
            return True, "interpreter -c/-e program runs a publishing command (git push, gh pr create, etc.)"

    return False, ""


def main():
    """
    Main entry point. Exit codes:
      - 0: allow the tool call
      - 2: block the tool call
    """
    try:
        # Parse CLI argument
        if len(sys.argv) < 3 or sys.argv[1] != "--agent":
            print("Usage: flow-bash-guard.py --agent <flow-implement|flow-review|auto>", file=sys.stderr)
            sys.exit(2)

        agent = sys.argv[2]
        if agent not in ("flow-implement", "flow-review", "auto"):
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

            # If agent_type is missing or not a flow agent, allow
            if not agent_type or agent_type not in ("flow-implement", "flow-review"):
                print(f"Auto mode: agent_type '{agent_type}' is not a flow agent, allowing", file=sys.stderr)
                sys.exit(0)

            # Use the agent_type from payload
            agent = agent_type

        # Only guard flow-implement; pass through other agents
        if agent != "flow-implement":
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

        # Check for wrapper forms and publishing verbs
        is_blocked, reason = check_command(command)
        if is_blocked:
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
