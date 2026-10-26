#!/usr/bin/env python3
"""
PreToolUse hook for flow-implement and flow-review agents: validates Skill invocations.

Threat model: prevent accidental or casual skill misuse. Fail closed on any ambiguity.
"""

import json
import sys

# Allowlist: maps agent name to the set of allowed Skill names.
ALLOWED_SKILLS = {
    "flow-implement": {"implement-with-haiku"},
    "flow-review": {"expert-review"},
}


def decide(payload, agent):
    """
    Validate a Skill invocation against the agent's allowlist.

    Returns: (exit_code: int, message: str)
    - exit_code 0: allow
    - exit_code 2: block
    message is empty on allow, otherwise a short bounded error message.
    """
    # Validate payload structure
    if not isinstance(payload, dict):
        return 2, "Invalid payload: not a dict"

    tool_name = payload.get("tool_name")
    if tool_name is None:
        return 2, "Missing tool_name in payload"

    if tool_name != "Skill":
        return 0, ""  # Not our tool; pass through

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 2, "Invalid tool_input: not a dict"

    skill = tool_input.get("skill")
    if not isinstance(skill, str) or not skill:
        return 2, "Missing or empty skill in tool_input"

    # Strip leading "/" if present
    skill = skill.lstrip("/")

    # Check allowlist
    allowed = ALLOWED_SKILLS.get(agent, set())
    if skill not in allowed:
        return 2, f"Skill '{skill}' not allowed for {agent}"

    return 0, ""


def main():
    try:
        # Parse CLI argument
        if len(sys.argv) < 3 or sys.argv[1] != "--agent":
            print("Usage: flow-skill-guard.py --agent <flow-implement|flow-review>", file=sys.stderr)
            sys.exit(2)

        agent = sys.argv[2]

        # Parse JSON payload from stdin
        payload = json.load(sys.stdin)

        # Scoping: if the payload has agent_type and it doesn't match our agent, allow it
        agent_type = payload.get("agent_type")
        if agent_type and isinstance(agent_type, str) and agent_type != agent:
            sys.exit(0)

        # Validate and decide
        code, message = decide(payload, agent)
        if message:
            print(message, file=sys.stderr)
        sys.exit(code)

    except json.JSONDecodeError:
        print("Failed to parse JSON payload", file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        print(f"Unexpected error in hook: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
