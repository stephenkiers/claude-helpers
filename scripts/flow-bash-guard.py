#!/usr/bin/env python3
"""
PreToolUse hook for flow-implement agent: best-effort Bash command guard.

Threat model: prevent accidental shell wrappers and publishing commands.
Best-effort control, not a sandbox: write-a-script-then-run, `python3 -c`, and a nested
general-purpose agent bypass it. See docs/adr/0008.

This script refuses:
  - Top-level shell wrappers (sh -c, bash -c, eval, xargs git/gh, env ... git/gh, etc.)
  - Publishing verbs (git push, gh pr create, gh pr merge, etc.)
"""

import json
import os
import re
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
    "git push",
    "git remote add",
    "git remote set-url",
    "gh pr create",
    "gh pr merge",
    "gh pr edit",
    "gh pr comment",
    "gh pr close",
    "gh pr review",
    "gh issue create",
    "gh issue edit",
    "gh issue comment",
    "gh issue close",
    "gh release create",
    "gh repo create",
    "gh repo delete",
    "gh gist create",
    "gh workflow run",
)

# Split on shell metacharacters
_METACHAR_RE = re.compile(r"([;&|`\n])")


def tokenize(command):
    """Split command into tokens by whitespace and shell metacharacters."""
    # Split on metacharacters, keeping them as separate tokens
    parts = _METACHAR_RE.split(command)
    tokens = []
    for part in parts:
        if part in ("", ";", "&", "|", "`", "\n"):
            # Skip empty and pure metachar tokens; we'll process them as separators
            if part and part not in (";", "&", "|", "`", "\n"):
                tokens.append(part)
        else:
            # Split on whitespace and strip VAR=val assignments from the front
            words = part.split()
            for word in words:
                # Skip VAR=val assignments
                if "=" in word and not any(c in word.split("=")[0] for c in " \t;&|`\n"):
                    continue
                if word:
                    tokens.append(word)
    return tokens


def check_wrapper_forms(command):
    """Check if command contains forbidden wrapper forms."""
    tokens = tokenize(command)

    # Check for wrapper patterns in sequence
    for i in range(len(tokens)):
        token = tokens[i]

        # Handle quoted and path-qualified forms
        token_basename = os.path.basename(token.strip('\'"')).lower()

        # sh -c, bash -c, zsh -c
        if token_basename in ("sh", "bash", "zsh"):
            if i + 1 < len(tokens) and tokens[i + 1] == "-c":
                return True

        # eval
        if token_basename == "eval":
            return True

        # xargs git or xargs gh
        if token_basename == "xargs":
            for j in range(i + 1, len(tokens)):
                if tokens[j] in ("git", "gh"):
                    return True

        # env ... git or env ... gh (any env call followed eventually by git/gh)
        if token_basename == "env":
            for j in range(i + 1, len(tokens)):
                j_basename = os.path.basename(tokens[j].strip('\'"')).lower()
                if j_basename in ("git", "gh"):
                    return True

        # git -c, git --git-dir, git --exec-path
        if token_basename == "git":
            j = i + 1
            while j < len(tokens):
                tok = tokens[j]
                if tok == "-C" or tok == "-c":
                    j += 2
                    continue
                if tok.startswith("-C"):
                    j += 1
                    continue
                if tok.startswith("-c") and len(tok) > 2:
                    j += 1
                    continue
                # Now tok is the git subcommand
                if tok in ("-c", "--git-dir", "--exec-path") or tok.startswith("--git-dir") or tok.startswith("--exec-path"):
                    return True
                break

        # gh api with non-GET method
        if token_basename == "gh":
            j = i + 1
            # Look for 'api' subcommand
            if j < len(tokens) and tokens[j] == "api":
                j += 1
                # Check for -X or --method flags
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

    return False


def check_publishing_verbs(command):
    """Check if command contains forbidden publishing verbs."""
    tokens = tokenize(command)

    # Rebuild the command string for verb matching, being careful about -C
    i = 0
    while i < len(tokens):
        token = tokens[i]
        token_basename = os.path.basename(token.strip('\'"')).lower()

        if token_basename == "git":
            j = i + 1
            # Skip git -C <path> if present
            if j < len(tokens) and tokens[j] == "-C":
                j += 2
            elif j < len(tokens) and tokens[j].startswith("-C"):
                j += 1

            # Now j should point to the verb
            if j < len(tokens):
                verb_segment = tokens[j].lower()
                for verb in PUBLISHING_VERBS:
                    if verb.startswith("git "):
                        expected_verb = verb.split(" ", 1)[1]
                        if verb_segment == expected_verb or verb_segment.startswith(expected_verb):
                            return True

        elif token_basename == "gh":
            # Reconstruct "gh <subcommand> <verb>" sequence
            if i + 2 < len(tokens):
                subcommand = tokens[i + 1].lower()
                verb = tokens[i + 2].lower()
                for pub_verb in PUBLISHING_VERBS:
                    if pub_verb.startswith("gh "):
                        parts = pub_verb.split()
                        if len(parts) >= 3 and parts[0] == "gh" and parts[1] == subcommand and parts[2] == verb:
                            return True

        i += 1

    return False


def main():
    try:
        # Parse CLI argument
        if len(sys.argv) < 3 or sys.argv[1] != "--agent":
            print("Usage: flow-bash-guard.py --agent flow-implement", file=sys.stderr)
            sys.exit(2)

        agent = sys.argv[2]

        # Only guard flow-implement; pass through other agents
        if agent != "flow-implement":
            sys.exit(0)

        # Parse JSON payload from stdin
        payload = json.load(sys.stdin)

        # Scoping: if the payload has agent_type and it doesn't match flow-implement, allow it
        agent_type = payload.get("agent_type")
        if agent_type and isinstance(agent_type, str) and agent_type != "flow-implement":
            sys.exit(0)

        tool_name = payload.get("tool_name")
        if tool_name is None:
            sys.exit(2)

        if tool_name != "Bash":
            sys.exit(0)  # Not a Bash call; allow

        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            sys.exit(2)

        command = tool_input.get("command")
        if not isinstance(command, str) or not command:
            sys.exit(2)

        # Check for wrapper forms
        if check_wrapper_forms(command):
            print("BLOCKED: shell wrappers (sh -c, bash -c, eval, etc.) are not allowed", file=sys.stderr)
            sys.exit(2)

        # Check for publishing verbs
        if check_publishing_verbs(command):
            print("BLOCKED: publishing commands (git push, gh pr create, etc.) are not allowed", file=sys.stderr)
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
