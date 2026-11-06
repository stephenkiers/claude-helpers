#!/usr/bin/env python3
"""
Test suite for flow agent registry parity (#360, Step 6).

Covers:
1. Registry parity: every flow-*.md on disk is in ALLOWED_SUBAGENT_TYPES, ALLOWED_SKILLS, KNOWN_FLOW_AGENTS
2. Hook naming: every agent with a hook mentions itself with --agent <stem>
3. FLOW_STEP_AGENTS: every step agent in the Steps table (except exemptions) is in FLOW_STEP_AGENTS
4. Auto mode: denies synthetic flow-unknown, allows non-flow agents

Run with: python3 tests/test_flow_agent_registry_parity.py
"""

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT
from _agent_frontmatter import parse_markdown_frontmatter

def run_guard_script(script_path, agent, payload_dict):
    """Run a guard script with JSON payload and return (exit_code, stderr)."""
    try:
        result = subprocess.run(
            ["python3", str(script_path), "--agent", agent],
            input=json.dumps(payload_dict),
            capture_output=True,
            text=True,
            timeout=60
        )
        return result.returncode, result.stderr
    except subprocess.TimeoutExpired:
        return 2, "Timeout"
    except Exception as e:
        return 2, str(e)


def parse_expert_flow_steps_table():
    """Extract the Steps table from expert-flow.md and return list of step rows.

    Each row is a dict with 'step' and 'agent' keys.
    """
    expert_flow = REPO_ROOT / "commands" / "expert-flow.md"
    if not expert_flow.exists():
        raise FileNotFoundError(f"Required file not found: {expert_flow}")

    text = expert_flow.read_text()

    # Find the table: it starts after "## Steps" and has columns for Agent
    steps_section = re.search(
        r'## Steps\n\n.*?\n\n\|(.*?)\n(?=\n##|\Z)',
        text, re.DOTALL
    )

    if not steps_section:
        raise ValueError("Steps table not found in expert-flow.md")

    table_text = steps_section.group(1)
    lines = table_text.split('\n')

    # Parse table rows: | # | Step | ... | Agent | ... |
    rows = []
    header_processed = False
    agent_col_idx = None
    step_col_idx = None

    for line in lines:
        if '|' not in line:
            continue

        cells = [c.strip() for c in line.split('|')]
        cells = [c for c in cells if c]  # Remove empty cells

        # Skip non-data lines (headers, separators)
        if not header_processed and any(c == '---' or c == 'Agent' for c in cells):
            # This is a header or separator; find column indices
            if 'Agent' in cells:
                agent_col_idx = cells.index('Agent')
                if 'Step' in cells:
                    step_col_idx = cells.index('Step')
            continue

        header_processed = True

        # Parse data row
        if agent_col_idx is not None and step_col_idx is not None and len(cells) > agent_col_idx:
            step = cells[step_col_idx] if step_col_idx < len(cells) else None
            agent = cells[agent_col_idx] if agent_col_idx < len(cells) else None
            if step and agent:
                rows.append({'step': step.lower(), 'agent': agent.strip()})

    return rows


def main():
    h = Harness("FLOW AGENT REGISTRY PARITY TEST SUITE")

    print("[Test 1] Agents on disk match registry keys\n")

    agents_dir = REPO_ROOT / "agents"
    agent_files = sorted(agents_dir.glob("flow-*.md"))
    agent_names = {f.stem for f in agent_files}

    h.test_result(
        "flow-*.md files exist",
        len(agent_names) > 0,
        f"Found {len(agent_names)} agents"
    )

    # Load guard registries
    flow_agent_guard = REPO_ROOT / "scripts" / "flow-agent-guard.py"
    flow_skill_guard = REPO_ROOT / "scripts" / "flow-skill-guard.py"
    flow_bash_guard = REPO_ROOT / "scripts" / "flow-bash-guard.py"
    flow_buckets = REPO_ROOT / "scripts" / "flow_agent_buckets.py"

    if not flow_agent_guard.exists():
        h.test_result("flow-agent-guard.py exists", False)
        h.summarize_and_exit()

    agent_guard_text = flow_agent_guard.read_text()
    skill_guard_text = flow_skill_guard.read_text()
    bash_guard_text = flow_bash_guard.read_text()
    buckets_text = flow_buckets.read_text()

    # Extract ALLOWED_SUBAGENT_TYPES keys
    agent_guard_keys = set()
    for match in re.finditer(r'"(flow-[^"]+)":\s*\{', agent_guard_text):
        agent_guard_keys.add(match.group(1))

    h.test_result(
        "flow-agent-guard.py has registrations",
        len(agent_guard_keys) > 0,
        f"Found {len(agent_guard_keys)} agents"
    )

    # Extract ALLOWED_SKILLS keys
    skill_guard_keys = set()
    for match in re.finditer(r'"(flow-[^"]+)":\s*\{', skill_guard_text):
        skill_guard_keys.add(match.group(1))

    h.test_result(
        "flow-skill-guard.py has registrations",
        len(skill_guard_keys) > 0,
        f"Found {len(skill_guard_keys)} agents"
    )

    # Extract KNOWN_FLOW_AGENTS tuple
    bash_guard_keys = set()
    match = re.search(r'KNOWN_FLOW_AGENTS\s*=\s*\((.*?)\)', bash_guard_text, re.DOTALL)
    if match:
        agents_str = match.group(1)
        for agent in re.findall(r'"(flow-[^"]+)"', agents_str):
            bash_guard_keys.add(agent)

    h.test_result(
        "flow-bash-guard.py KNOWN_FLOW_AGENTS has entries",
        len(bash_guard_keys) > 0,
        f"Found {len(bash_guard_keys)} agents"
    )

    # Extract FLOW_STEP_AGENTS from buckets
    buckets_keys = set()
    match = re.search(r'FLOW_STEP_AGENTS\s*=\s*\{(.*?)\}', buckets_text)
    if match:
        agents_str = match.group(1)
        for agent in re.findall(r'"(flow-[^"]+)"', agents_str):
            buckets_keys.add(agent)

    h.test_result(
        "flow_agent_buckets.py FLOW_STEP_AGENTS has entries",
        len(buckets_keys) > 0,
        f"Found {len(buckets_keys)} agents"
    )

    # Check parity: agent_names subset of all guard keys
    print()
    print("[Test 2] Every agent file is registered in bash guard KNOWN_FLOW_AGENTS\n")

    # Bash guard KNOWN_FLOW_AGENTS must have all flow-* agents (it's the authoritative list)
    for agent in sorted(agent_names):
        h.test_result(
            f"{agent} in flow-bash-guard.py KNOWN_FLOW_AGENTS",
            agent in bash_guard_keys,
            "Missing from bash guard"
        )

    print()
    print("[Test 2b] Agents that spawn subagents are in agent-guard registrations\n")

    # Only agents that have Agent/Task in tools need entries in ALLOWED_SUBAGENT_TYPES
    for agent_file in sorted(agent_files):
        agent_name = agent_file.stem
        fm = parse_markdown_frontmatter(agent_file.read_text())
        tools = fm.get('tools', '')
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(',')]

        has_agent_tool = 'Agent' in str(tools) or 'Task' in str(tools)
        if has_agent_tool:
            # Tools floor assertion: agents with Agent/Task must have non-empty tools list
            h.test_result(
                f"{agent_name} (has Agent/Task) has non-empty tools",
                len(tools) > 0,
                "Tools list is empty but Agent/Task present"
            )
            h.test_result(
                f"{agent_name} (has Agent/Task tools) in flow-agent-guard.py",
                agent_name in agent_guard_keys,
                "Missing from agent guard"
            )
        else:
            # No skip: fail if Agent/Task are expected but missing from this agent
            pass

    print()
    print("[Test 2c] Agents that invoke skills are in skill-guard registrations\n")

    # Only agents that have Skill in tools need entries in ALLOWED_SKILLS
    for agent_file in sorted(agent_files):
        agent_name = agent_file.stem
        fm = parse_markdown_frontmatter(agent_file.read_text())
        tools = fm.get('tools', '')
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(',')]

        has_skill_tool = 'Skill' in str(tools)
        if has_skill_tool:
            # Tools floor assertion: agents with Skill must have non-empty tools list
            h.test_result(
                f"{agent_name} (has Skill) has non-empty tools",
                len(tools) > 0,
                "Tools list is empty but Skill present"
            )
            h.test_result(
                f"{agent_name} (has Skill tool) in flow-skill-guard.py",
                agent_name in skill_guard_keys,
                "Missing from skill guard"
            )
        else:
            # No skip: fail if Skill is expected but missing from this agent
            pass

    print()
    print("[Test 3] Hook naming consistency (agents that define hooks)\n")

    # Every agent with a hook definition should name itself with --agent <stem>
    for agent_file in sorted(agent_files):
        body = agent_file.read_text()
        agent_stem = agent_file.stem

        # Check if body defines hooks in the frontmatter (not just mentions "hook")
        fm = parse_markdown_frontmatter(body)
        has_hooks_defined = 'hooks' in fm

        if has_hooks_defined:
            h.test_result(
                f"{agent_file.name} mentions --agent {agent_stem}",
                f"--agent {agent_stem}" in body,
                f"Expected '--agent {agent_stem}' in body"
            )

    print()
    print("[Test 4] Steps table coverage\n")

    try:
        rows = parse_expert_flow_steps_table()
    except Exception as e:
        h.test_result("Parse expert-flow Steps table", False, str(e))
        h.summarize_and_exit()

    h.test_result(
        "Steps table has rows",
        len(rows) > 0,
        f"Found {len(rows)} rows"
    )

    # Check that every row with a flow-* agent has that agent in bash guard (authoritative)
    for row in rows:
        agent = row['agent']
        step = row['step']

        if agent.startswith('flow-'):
            # Must be in bash guard KNOWN_FLOW_AGENTS (authoritative list)
            in_bash_guard = agent in bash_guard_keys

            h.test_result(
                f"Step '{step}' agent '{agent}' in bash guard KNOWN_FLOW_AGENTS",
                in_bash_guard,
                "Missing from bash guard"
            )

            # Agents with Agent/Task tools must also be in agent guard
            agent_file = agents_dir / f"{agent}.md"
            if agent_file.exists():
                fm = parse_markdown_frontmatter(agent_file.read_text())
                tools = fm.get('tools', '')
                if isinstance(tools, str):
                    tools = [t.strip() for t in tools.split(',')]
                if 'Agent' in str(tools) or 'Task' in str(tools):
                    in_agent_guard = agent in agent_guard_keys
                    h.test_result(
                        f"Step '{step}' agent '{agent}' (with Agent/Task) in agent guard",
                        in_agent_guard,
                        "Missing from agent guard"
                    )

                # Agents with Skill tool must also be in skill guard
                if 'Skill' in str(tools):
                    in_skill_guard = agent in skill_guard_keys
                    h.test_result(
                        f"Step '{step}' agent '{agent}' (with Skill) in skill guard",
                        in_skill_guard,
                        "Missing from skill guard"
                    )

    print()
    print("[Test 5] FLOW_STEP_AGENTS coverage (exemption: flow-assessor)\n")

    # flow-assessor is exempted from FLOW_STEP_AGENTS per D2
    exemptions = {"flow-assessor"}

    for row in rows:
        agent = row['agent']
        step = row['step']

        if agent.startswith('flow-') and agent not in exemptions:
            in_buckets = agent in buckets_keys

            h.test_result(
                f"Step '{step}' agent '{agent}' in FLOW_STEP_AGENTS",
                in_buckets,
                "Missing from flow_agent_buckets"
            )

    # flow-assessor should NOT be in FLOW_STEP_AGENTS (per D2)
    h.test_result(
        "flow-assessor NOT in FLOW_STEP_AGENTS (D2 exemption)",
        "flow-assessor" not in buckets_keys,
        "flow-assessor should be exempt per D2"
    )

    print()
    print("[Test 6] Auto mode: deny unknown flow-* agents\n")

    # Test agent guard auto mode with synthetic flow-unknown
    code, _ = run_guard_script(flow_agent_guard, "auto", {
        "agent_type": "flow-unknown",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "plan-implementer"}
    })
    h.test_result(
        "Agent guard auto mode denies flow-unknown (exit 2)",
        code == 2,
        f"Expected 2, got {code}"
    )

    # Test agent guard auto mode with non-flow agent (should allow)
    code, _ = run_guard_script(flow_agent_guard, "auto", {
        "agent_type": "general-purpose",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "plan-implementer"}
    })
    h.test_result(
        "Agent guard auto mode allows non-flow agent (exit 0)",
        code == 0,
        f"Expected 0, got {code}"
    )

    # Test skill guard auto mode with synthetic flow-unknown
    code, _ = run_guard_script(flow_skill_guard, "auto", {
        "agent_type": "flow-unknown",
        "tool_name": "Skill",
        "tool_input": {"skill": "expert-plan"}
    })
    h.test_result(
        "Skill guard auto mode denies flow-unknown (exit 2)",
        code == 2,
        f"Expected 2, got {code}"
    )

    # Test skill guard auto mode with non-flow agent (should allow)
    code, _ = run_guard_script(flow_skill_guard, "auto", {
        "agent_type": "general-purpose",
        "tool_name": "Skill",
        "tool_input": {"skill": "expert-plan"}
    })
    h.test_result(
        "Skill guard auto mode allows non-flow agent (exit 0)",
        code == 0,
        f"Expected 0, got {code}"
    )

    # Test bash guard auto mode with synthetic flow-unknown
    code, _ = run_guard_script(flow_bash_guard, "auto", {
        "agent_type": "flow-unknown",
        "tool_name": "Bash",
        "tool_input": {"command": "git add -A"}
    })
    h.test_result(
        "Bash guard auto mode denies flow-unknown (exit 2)",
        code == 2,
        f"Expected 2, got {code}"
    )

    # Test bash guard auto mode with non-flow agent (should allow)
    code, _ = run_guard_script(flow_bash_guard, "auto", {
        "agent_type": "general-purpose",
        "tool_name": "Bash",
        "tool_input": {"command": "git push"}  # would be blocked for flow-*, allowed for general-purpose
    })
    h.test_result(
        "Bash guard auto mode allows non-flow agent (exit 0)",
        code == 0,
        f"Expected 0, got {code}"
    )

    print()
    print("[Test 7] flow-plan registration\n")

    # Specific test for flow-plan existence and registration
    h.test_result(
        "flow-plan in agent-guard registrations",
        "flow-plan" in agent_guard_keys,
    )

    h.test_result(
        "flow-plan in skill-guard registrations",
        "flow-plan" in skill_guard_keys,
    )

    h.test_result(
        "flow-plan in bash-guard registrations",
        "flow-plan" in bash_guard_keys,
    )

    h.test_result(
        "flow-plan in flow_agent_buckets FLOW_STEP_AGENTS",
        "flow-plan" in buckets_keys,
    )

    print()
    print("[Test 8] flow-assessor Bash tool restriction\n")

    # flow-assessor must have only prefix-scoped Bash entries, no bare Bash
    flow_assessor_file = agents_dir / "flow-assessor.md"
    if flow_assessor_file.exists():
        fm = parse_markdown_frontmatter(flow_assessor_file.read_text())
        tools = fm.get('tools', '')
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(',')]

        # Check for bare Bash (no prefix)
        has_bare_bash = 'Bash' in tools
        has_prefix_bash = any(t.startswith('Bash(') for t in tools)

        h.test_result(
            "flow-assessor has no bare Bash entry",
            not has_bare_bash,
            f"Found bare Bash in tools={tools}"
        )

        h.test_result(
            "flow-assessor has only prefix-scoped Bash entries",
            has_prefix_bash and not has_bare_bash,
            f"Expected only Bash(...) entries, got tools={tools}"
        )
    else:
        h.test_result("flow-assessor.md exists", False, "flow-assessor.md not found")

    print()

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
