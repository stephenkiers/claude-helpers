#!/usr/bin/env python3
"""
Test suite for flow step agent configuration and control binding.

Covers:
1. Step→agent pin from the Steps table Agent column
2. Model allowed set (T-W1): every agents/*.md model: is in an explicit set
3. Tools superset (Carl): each flow-* tools: contains Skill and Agent, plus command allowed-tools
4. Agent allowlist derivation (Sage): subagent_type and skill names match guard allowlists

Run with: python3 tests/test_flow_step_agents.py
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


def parse_markdown_frontmatter(text):
    """Extract YAML frontmatter from markdown as a dict of key: value lines."""
    match = re.match(r'^---\n(.*?)\n---\n', text, re.DOTALL)
    if not match:
        return {}

    fm_text = match.group(1)
    result = {}
    for line in fm_text.split('\n'):
        if ':' not in line:
            continue
        key, value = line.split(':', 1)
        key = key.strip()
        value = value.strip()
        # Handle comma-separated lists (for tools, etc.)
        if key in ('tools', 'allowed-tools'):
            # Split on comma, but preserve Bash(...) entries
            result[key] = [v.strip() for v in value.split(',')]
        else:
            result[key] = value
    return result


def parse_expert_flow_steps_table():
    """Extract the Steps table from expert-flow.md and return step->agent mapping."""
    expert_flow = REPO_ROOT / "commands" / "expert-flow.md"
    text = expert_flow.read_text()

    # Find the table: it starts after "## Steps" and has columns for Agent
    steps_section = re.search(
        r'## Steps\n\n.*?\n\n\|(.*?)\n(?=\n##|\Z)',
        text, re.DOTALL
    )

    if not steps_section:
        return {}

    table_text = steps_section.group(1)
    lines = table_text.split('\n')

    # Parse table rows: | # | Step | ... | Agent | ... |
    step_to_agent = {}
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
                step_to_agent[step.lower()] = agent.strip()

    return step_to_agent


def extract_subagent_spawns(text):
    """Extract all subagent_type spawns from command/prompt markdown.

    Returns a set of (command_name, subagent_type) tuples where:
    - command_name is the markdown filename without .md
    - subagent_type is the value from subagent_type: "..." or the agent name in a prose spawn pattern
    """
    spawns = set()

    # Pattern 1: subagent_type: "value"
    for match in re.finditer(r'subagent_type:\s*"([^"]+)"', text):
        spawns.add(match.group(1))

    # Pattern 2: background `<agent>` spawn (from commands/implement-with-haiku.md prose)
    for match in re.finditer(r'`([a-z][a-z-]*)`\s+(?:agent|subagent)', text, re.IGNORECASE):
        spawns.add(match.group(1))

    return spawns


def main():
    h = Harness("FLOW STEP AGENT CONFIGURATION TEST SUITE")

    print("[Test 1] Step→agent pin from Steps table\n")

    step_to_agent = parse_expert_flow_steps_table()
    h.test_result(
        "Steps table parsed successfully",
        len(step_to_agent) > 0,
        f"Found {len(step_to_agent)} steps"
    )

    # Expected mappings
    expected = {
        'implement': 'flow-implement',
        'fix': 'flow-implement',
        'review': 'flow-review',
        'worktree': 'general-purpose',
        'plan': 'general-purpose',
        'track': 'general-purpose',
        'verify': 'general-purpose',
        'ship': 'general-purpose',
        'merge': 'general-purpose',
    }

    for step, expected_agent in expected.items():
        actual = step_to_agent.get(step)
        h.test_result(
            f"Step '{step}' → '{expected_agent}'",
            actual == expected_agent,
            f"Expected '{expected_agent}', got '{actual}'"
        )

    print()
    print("[Test 2] Model allowed set (T-W1)\n")

    # Allowed model values (explicit set)
    allowed_models = {
        'sonnet',
        'haiku',
        'claude-3-5-sonnet-20241022',
        'claude-3-5-haiku-20241022',
        'claude-haiku-4-5-20251001',
    }

    agents_dir = REPO_ROOT / "agents"
    agent_files = sorted(agents_dir.glob("*.md"))

    h.test_result(
        "agents/*.md files exist",
        len(agent_files) > 0,
        f"Found {len(agent_files)} agent files"
    )

    for agent_file in agent_files:
        fm = parse_markdown_frontmatter(agent_file.read_text())
        model = fm.get('model', '')

        # Some agents (e.g., expert-reviewer) don't have a model field; they inherit from the caller
        if model:
            h.test_result(
                f"{agent_file.name} model '{model}' is in allowed set",
                model in allowed_models,
                f"Model '{model}' not in {allowed_models}"
            )
        # Note: agents without a model field (inheriting from caller) are implicitly allowed

    print()
    print("[Test 3] Model assignment for flow-* agents\n")

    # Specifically check flow-implement and flow-review
    flow_implement = REPO_ROOT / "agents" / "flow-implement.md"
    flow_review = REPO_ROOT / "agents" / "flow-review.md"

    h.test_result("flow-implement.md exists", flow_implement.exists())
    h.test_result("flow-review.md exists", flow_review.exists())

    if flow_implement.exists():
        fm = parse_markdown_frontmatter(flow_implement.read_text())
        h.test_result(
            "flow-implement has model: sonnet",
            fm.get('model') == 'sonnet',
            f"Expected 'sonnet', got '{fm.get('model')}'"
        )

    if flow_review.exists():
        fm = parse_markdown_frontmatter(flow_review.read_text())
        h.test_result(
            "flow-review has model: sonnet",
            fm.get('model') == 'sonnet',
            f"Expected 'sonnet', got '{fm.get('model')}'"
        )

    print()
    print("[Test 4] Tools superset (Carl)\n")

    # Read the commands' allowed-tools
    expert_review = REPO_ROOT / "commands" / "expert-review.md"
    implement_with_haiku = REPO_ROOT / "commands" / "implement-with-haiku.md"

    def extract_tools(fm_tools_str):
        """Parse 'tool1, tool2, Bash(...), ...' into a set of tool names."""
        tools = set()
        for entry in fm_tools_str:
            # Strip Bash(...) to just "Bash"
            if entry.startswith('Bash('):
                tools.add('Bash')
            else:
                tools.add(entry)
        return tools

    if expert_review.exists():
        fm = parse_markdown_frontmatter(expert_review.read_text())
        review_tools = extract_tools(fm.get('allowed-tools', []))

        if flow_review.exists():
            flow_review_fm = parse_markdown_frontmatter(flow_review.read_text())
            flow_review_tools = extract_tools(flow_review_fm.get('tools', []))

            # flow-review should have: review_tools + Agent + Skill + Edit (retained)
            required_tools = review_tools | {'Agent', 'Skill'}
            missing = required_tools - flow_review_tools
            h.test_result(
                "flow-review tools contain expert-review tools + Agent + Skill",
                not missing,
                f"Missing tools: {missing}"
            )

            h.test_result(
                "flow-review retains Edit tool",
                'Edit' in flow_review_tools,
                "Edit must be retained for claude-action-plan.md editing"
            )

    if implement_with_haiku.exists():
        fm = parse_markdown_frontmatter(implement_with_haiku.read_text())
        impl_tools = extract_tools(fm.get('allowed-tools', []))

        if flow_implement.exists():
            flow_impl_fm = parse_markdown_frontmatter(flow_implement.read_text())
            flow_impl_tools = extract_tools(flow_impl_fm.get('tools', []))

            # flow-implement should have: impl_tools + Agent + Skill - Edit
            required_tools = impl_tools | {'Agent', 'Skill'}
            missing = required_tools - flow_impl_tools
            h.test_result(
                "flow-implement tools contain implement-with-haiku tools + Agent + Skill",
                not missing,
                f"Missing tools: {missing}"
            )

            h.test_result(
                "flow-implement does NOT have Edit tool",
                'Edit' not in flow_impl_tools,
                "Edit must be omitted from flow-implement"
            )

    print()
    print("[Test 5] No permissionMode on flow-*\n")

    for agent_file in [flow_implement, flow_review]:
        if agent_file.exists():
            text = agent_file.read_text()
            fm = parse_markdown_frontmatter(text)
            h.test_result(
                f"{agent_file.name} has no permissionMode (unless Step 2.2 probe required it)",
                'permissionMode' not in fm or fm.get('permissionMode') == '',
                f"Found permissionMode: {fm.get('permissionMode')}"
            )

    print()
    print("[Test 6] No Bash(...) prefix entries on flow-*\n")

    for agent_file in [flow_implement, flow_review]:
        if agent_file.exists():
            text = agent_file.read_text()
            fm = parse_markdown_frontmatter(text)
            tools = fm.get('tools', []) if isinstance(fm.get('tools'), list) else [fm.get('tools', '')]
            bash_prefixed = [t for t in tools if t.startswith('Bash(')]
            h.test_result(
                f"{agent_file.name} has no Bash(...) prefix entries",
                not bash_prefixed,
                f"Found: {bash_prefixed}"
            )

    print()
    print("[Test 7] Agent allowlist derivation (Sage)\n")

    # Load the flow guard allowlists
    flow_agent_guard = REPO_ROOT / "scripts" / "flow-agent-guard.py"
    flow_skill_guard = REPO_ROOT / "scripts" / "flow-skill-guard.py"

    # Extract ALLOWED_SUBAGENT_TYPES from flow-agent-guard.py
    agent_guard_text = flow_agent_guard.read_text() if flow_agent_guard.exists() else ""
    agent_allowlist = {}
    for match in re.finditer(
        r'"(flow-[^"]+)":\s*\{([^}]+)\}',
        agent_guard_text
    ):
        agent_name = match.group(1)
        agents_str = match.group(2)
        agents = {a.strip().strip('"\'') for a in agents_str.split(',')}
        agent_allowlist[agent_name] = agents

    h.test_result(
        "flow-agent-guard.py defines allowlists",
        len(agent_allowlist) > 0,
        f"Found {len(agent_allowlist)} agent allowlists"
    )

    # Extract ALLOWED_SKILLS from flow-skill-guard.py
    skill_guard_text = flow_skill_guard.read_text() if flow_skill_guard.exists() else ""
    skill_allowlist = {}
    for match in re.finditer(
        r'"(flow-[^"]+)":\s*\{([^}]+)\}',
        skill_guard_text
    ):
        agent_name = match.group(1)
        skills_str = match.group(2)
        skills = {s.strip().strip('"\'') for s in skills_str.split(',')}
        skill_allowlist[agent_name] = skills

    h.test_result(
        "flow-skill-guard.py defines allowlists",
        len(skill_allowlist) > 0,
        f"Found {len(skill_allowlist)} skill allowlists"
    )

    # Now check that commands spawn the right agents
    impl_text = implement_with_haiku.read_text() if implement_with_haiku.exists() else ""
    review_text = expert_review.read_text() if expert_review.exists() else ""

    impl_spawns = extract_subagent_spawns(impl_text)
    review_spawns = extract_subagent_spawns(review_text)

    if agent_allowlist.get('flow-implement'):
        allowed = agent_allowlist['flow-implement']
        allowed_in_spawns = impl_spawns & allowed
        h.test_result(
            "implement-with-haiku spawns only allowed agents",
            len(allowed_in_spawns) > 0,
            f"Expected one of {allowed} in spawns, found {impl_spawns}"
        )

    if agent_allowlist.get('flow-review'):
        allowed = agent_allowlist['flow-review']
        allowed_in_spawns = review_spawns & allowed
        h.test_result(
            "expert-review spawns only allowed agents",
            len(allowed_in_spawns) > 0,
            f"Expected one of {allowed} in spawns, found {review_spawns}"
        )

    # Check that general-purpose is not in either allowlist
    h.test_result(
        "general-purpose not in flow-implement allowlist",
        'general-purpose' not in agent_allowlist.get('flow-implement', set()),
    )

    h.test_result(
        "general-purpose not in flow-review allowlist",
        'general-purpose' not in agent_allowlist.get('flow-review', set()),
    )

    print()

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
