#!/usr/bin/env python3
"""
PreToolUse hook for flow-implement agent: best-effort Bash command guard.

Threat model: prevent accidental shell wrappers and publishing commands.
Best-effort control, not a sandbox: write-a-script-then-run, `python3 -c`, and a nested
general-purpose agent bypass it. See docs/adr/0008.

This script refuses:
  - Top-level shell wrappers (sh -c, bash -c, eval, xargs git/gh, env ... git/gh, etc.)
  - Publishing verbs (git push, gh pr create, gh pr merge, etc.)

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

# Split on shell metacharacters and subshell/group syntax
_SEGMENT_RE = re.compile(r'(;|&&|\|\||&|\||`|$\(|[({]|\n)')


def split_segments(command):
    """Split command into segments on metacharacters, subshells, and grouping.

    Returns list of command segments, each separated by shell metacharacters.
    """
    # Use a simple heuristic: split on ; && || | & newline and subshell/group starts
    segments = []
    current = []
    i = 0
    while i < len(command):
        if i + 1 < len(command) and command[i:i+2] in ("&&", "||"):
            if current:
                segments.append("".join(current).strip())
                current = []
            i += 2
        elif command[i] in (";", "&", "|", "`", "\n", "(", "{"):
            if current:
                segments.append("".join(current).strip())
                current = []
            if command[i] in ("(", "{"):
                # Skip grouping; include until matching close
                depth = 1
                i += 1
                while i < len(command) and depth > 0:
                    if command[i] == "(" or command[i] == "{":
                        depth += 1
                    elif command[i] == ")" or command[i] == "}":
                        depth -= 1
                    i += 1
            else:
                i += 1
        elif i + 1 < len(command) and command[i:i+2] == "$(":
            # Skip $(...) subshell
            if current:
                segments.append("".join(current).strip())
                current = []
            depth = 1
            i += 2
            while i < len(command) and depth > 0:
                if command[i] == "(":
                    depth += 1
                elif command[i] == ")":
                    depth -= 1
                i += 1
        else:
            current.append(command[i])
            i += 1

    if current:
        segments.append("".join(current).strip())

    return [s for s in segments if s]


def tokenize_segment(segment):
    """Tokenize a single shell command segment using shlex.

    Fails closed on ValueError (unparseable). Returns None on parse failure.
    """
    try:
        return shlex.split(segment)
    except ValueError:
        # Unparseable (e.g., unbalanced quotes) -> block
        return None


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


def check_command(command):
    """Check if a command violates wrapper or verb rules.

    Returns: (is_blocked: bool, reason: str)
    - (True, reason): command is blocked
    - (False, ""): command is allowed
    """
    if not command or not isinstance(command, str):
        return True, "Empty or invalid command"

    segments = split_segments(command)

    for segment in segments:
        if not segment:
            continue

        tokens = tokenize_segment(segment)
        if tokens is None:
            # Parse error (e.g., unbalanced quotes) -> block
            return True, f"Unparseable command: {segment}"

        if is_wrapper_form(tokens):
            return True, "shell wrappers (sh -c, bash -c, eval, etc.) are not allowed"

        if is_publishing_verb(tokens):
            return True, "publishing commands (git push, gh pr create, etc.) are not allowed"

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

        # Parse JSON payload from stdin
        if not isinstance(sys.stdin, object):
            sys.exit(2)

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
