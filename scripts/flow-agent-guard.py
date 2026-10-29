#!/usr/bin/env python3
"""
PreToolUse hook for flow-implement and flow-review agents: validates subagent_type allowlist.

Threat model: prevent accidental or casual agent misuse. Fail closed on any ambiguity.
"""

import json
import sys

# Allowlist: maps agent name to the set of subagent_type values it may spawn.
ALLOWED_SUBAGENT_TYPES = {
    "flow-implement": {"plan-implementer"},
    "flow-review": {"expert-reviewer", "expert-scout"},
}


def decide(payload, agent):
    """
    Validate a subagent spawn against the agent's allowlist.

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

    if tool_name not in ("Agent", "Task"):
        return 0, ""  # Not our tool; pass through

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 2, "Invalid tool_input: not a dict"

    subagent_type = tool_input.get("subagent_type")
    if not isinstance(subagent_type, str) or not subagent_type:
        return 2, "Missing or empty subagent_type in tool_input"

    # Check allowlist
    allowed = ALLOWED_SUBAGENT_TYPES.get(agent, set())
    if subagent_type not in allowed:
        return 2, f"subagent_type '{subagent_type}' not allowed for {agent}"

    return 0, ""


def main():
    """
    Main entry point. Exit codes:
      - 0: allow the tool call
      - 2: block the tool call
    """
    try:
        # Parse CLI argument
        if len(sys.argv) < 3 or sys.argv[1] != "--agent":
            print("Usage: flow-agent-guard.py --agent <flow-implement|flow-review|auto>", file=sys.stderr)
            sys.exit(2)

        agent = sys.argv[2]

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
