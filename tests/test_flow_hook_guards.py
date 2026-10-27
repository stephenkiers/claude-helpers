#!/usr/bin/env python3
"""
Test suite for flow step agent hook guards.

Covers:
1. Hook guard exit codes: exit 2 on invalid payloads, 0 on allow, 2 on block
2. Agent guard: allowlist for subagent_type, both Agent and Task matchers
3. Skill guard: exactly one skill per agent
4. Bash guard: wrapper forms and publishing verbs (Q1 = C)
5. Bash guard drift guard: verify wrapper set consistency with ADR-0008

Run with: python3 tests/test_flow_hook_guards.py
"""

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


def run_guard_script(script_path, agent, payload_dict):
    """Run a guard script with JSON payload and return (exit_code, stderr)."""
    try:
        result = subprocess.run(
            ["python3", str(script_path), "--agent", agent],
            input=json.dumps(payload_dict),
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.returncode, result.stderr
    except subprocess.TimeoutExpired:
        return 2, "Timeout"
    except Exception as e:
        return 2, str(e)


def main():
    h = Harness("FLOW HOOK GUARD TEST SUITE")

    print("[Test 1] Hook guard script exit codes\n")

    flow_agent_guard = REPO_ROOT / "scripts" / "flow-agent-guard.py"
    flow_skill_guard = REPO_ROOT / "scripts" / "flow-skill-guard.py"
    flow_bash_guard = REPO_ROOT / "scripts" / "flow-bash-guard.py"

    h.test_result("flow-agent-guard.py exists", flow_agent_guard.exists())
    h.test_result("flow-skill-guard.py exists", flow_skill_guard.exists())
    h.test_result("flow-bash-guard.py exists", flow_bash_guard.exists())

    if not (flow_agent_guard.exists() and flow_skill_guard.exists() and flow_bash_guard.exists()):
        h.summarize_and_exit()

    print()
    print("[Test 2] Agent guard: invalid payloads exit 2\n")

    # Test empty payload
    code, _ = run_guard_script(flow_agent_guard, "flow-implement", {})
    h.test_result(
        "Agent guard exits 2 on empty payload",
        code == 2
    )

    # Test missing tool_name
    code, _ = run_guard_script(flow_agent_guard, "flow-implement", {"tool_input": {}})
    h.test_result(
        "Agent guard exits 2 on missing tool_name",
        code == 2
    )

    # Test missing tool_input
    code, _ = run_guard_script(flow_agent_guard, "flow-implement", {"tool_name": "Agent"})
    h.test_result(
        "Agent guard exits 2 on missing tool_input",
        code == 2
    )

    # Test missing subagent_type
    code, _ = run_guard_script(flow_agent_guard, "flow-implement", {
        "tool_name": "Agent",
        "tool_input": {}
    })
    h.test_result(
        "Agent guard exits 2 on missing subagent_type",
        code == 2
    )

    print()
    print("[Test 3] Agent guard: allowed subagent_type values\n")

    # flow-implement should allow plan-implementer
    code, _ = run_guard_script(flow_agent_guard, "flow-implement", {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "plan-implementer"}
    })
    h.test_result(
        "Agent guard exits 0 for flow-implement with plan-implementer",
        code == 0
    )

    # flow-implement should block general-purpose
    code, _ = run_guard_script(flow_agent_guard, "flow-implement", {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "general-purpose"}
    })
    h.test_result(
        "Agent guard exits 2 for flow-implement with general-purpose",
        code == 2
    )

    # flow-review should allow expert-reviewer
    code, _ = run_guard_script(flow_agent_guard, "flow-review", {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "expert-reviewer"}
    })
    h.test_result(
        "Agent guard exits 0 for flow-review with expert-reviewer",
        code == 0
    )

    # flow-review should allow expert-scout
    code, _ = run_guard_script(flow_agent_guard, "flow-review", {
        "tool_name": "Task",
        "tool_input": {"subagent_type": "expert-scout"}
    })
    h.test_result(
        "Agent guard exits 0 for flow-review with expert-scout (Task matcher)",
        code == 0
    )

    print()
    print("[Test 4] Skill guard: invalid payloads exit 2\n")

    code, _ = run_guard_script(flow_skill_guard, "flow-implement", {})
    h.test_result(
        "Skill guard exits 2 on empty payload",
        code == 2
    )

    code, _ = run_guard_script(flow_skill_guard, "flow-implement", {
        "tool_name": "Skill",
        "tool_input": {}
    })
    h.test_result(
        "Skill guard exits 2 on missing skill",
        code == 2
    )

    print()
    print("[Test 5] Skill guard: allowed skills per agent\n")

    code, _ = run_guard_script(flow_skill_guard, "flow-implement", {
        "tool_name": "Skill",
        "tool_input": {"skill": "implement-with-haiku"}
    })
    h.test_result(
        "Skill guard exits 0 for flow-implement with implement-with-haiku",
        code == 0
    )

    code, _ = run_guard_script(flow_skill_guard, "flow-implement", {
        "tool_name": "Skill",
        "tool_input": {"skill": "expert-review"}
    })
    h.test_result(
        "Skill guard exits 2 for flow-implement with expert-review",
        code == 2
    )

    code, _ = run_guard_script(flow_skill_guard, "flow-review", {
        "tool_name": "Skill",
        "tool_input": {"skill": "expert-review"}
    })
    h.test_result(
        "Skill guard exits 0 for flow-review with expert-review",
        code == 0
    )

    code, _ = run_guard_script(flow_skill_guard, "flow-review", {
        "tool_name": "Skill",
        "tool_input": {"skill": "implement-with-haiku"}
    })
    h.test_result(
        "Skill guard exits 2 for flow-review with implement-with-haiku",
        code == 2
    )

    print()
    print("[Test 6] Bash guard: wrapper forms exit 2 (Q1 = C)\n")

    wrapper_tests = [
        ("sh -c 'git push'", True, "sh -c"),
        ("bash -c 'echo hi'", True, "bash -c"),
        ("eval 'git push'", True, "eval"),
        ("xargs git push", True, "xargs git"),
        ("env X=1 git push", True, "env git"),
        ("git --git-dir=/tmp push", True, "git --git-dir"),
        ("git --exec-path=/tmp push", True, "git --exec-path"),
    ]

    for command, should_block, desc in wrapper_tests:
        code, _ = run_guard_script(flow_bash_guard, "flow-implement", {
            "tool_name": "Bash",
            "tool_input": {"command": command}
        })
        expected_code = 2 if should_block else 0
        h.test_result(
            f"Bash guard blocks '{desc}'",
            code == expected_code,
            f"Expected {expected_code}, got {code}"
        )

    print()
    print("[Test 7] Bash guard: publishing verbs exit 2\n")

    publish_tests = [
        ("git push origin main", True, "git push"),
        ("gh pr create --title 'test'", True, "gh pr create"),
        ("gh pr merge --squash", True, "gh pr merge"),
        ("gh pr edit --add-label bug", True, "gh pr edit"),
        ("gh issue create --title test", True, "gh issue create"),
    ]

    for command, should_block, desc in publish_tests:
        code, _ = run_guard_script(flow_bash_guard, "flow-implement", {
            "tool_name": "Bash",
            "tool_input": {"command": command}
        })
        expected_code = 2 if should_block else 0
        h.test_result(
            f"Bash guard blocks '{desc}'",
            code == expected_code,
            f"Expected {expected_code}, got {code}"
        )

    print()
    print("[Test 8] Bash guard: legitimate commands exit 0\n")

    legit_tests = [
        ("just check", "just check"),
        ("python3 tests/run_all.py", "python3 tests"),
        ("git add -A", "git add"),
        ("git commit -m 'test'", "git commit"),
        ("git apply patch.diff", "git apply"),
        ("gh api /repos/owner/repo", "gh api GET"),
    ]

    for command, desc in legit_tests:
        code, _ = run_guard_script(flow_bash_guard, "flow-implement", {
            "tool_name": "Bash",
            "tool_input": {"command": command}
        })
        h.test_result(
            f"Bash guard allows '{desc}'",
            code == 0,
            f"Expected 0, got {code}"
        )

    print()
    print("[Test 9] Bash guard drift guard: wrapper set consistency (Q1)\n")

    # Extract WRAPPER_FORMS from flow-bash-guard.py
    bash_guard_text = flow_bash_guard.read_text()
    wrapper_match = re.search(
        r'WRAPPER_FORMS\s*=\s*\((.*?)\)',
        bash_guard_text,
        re.DOTALL
    )

    guard_wrappers = set()
    if wrapper_match:
        wrapper_str = wrapper_match.group(1)
        for line in wrapper_str.split('\n'):
            line = line.strip()
            if line.startswith('"') or line.startswith("'"):
                wrapper = line.strip('",\' ')
                guard_wrappers.add(wrapper)

    h.test_result(
        "Bash guard defines WRAPPER_FORMS",
        len(guard_wrappers) > 0,
        f"Found {len(guard_wrappers)} wrappers"
    )

    # Extract wrapper block from ADR-0008
    adr_0008 = REPO_ROOT / "docs" / "adr" / "0008-machine-enforced-agent-guardrails.md"
    adr_wrappers = set()

    if adr_0008.exists():
        adr_text = adr_0008.read_text()
        # Look for marked blocks with wrapper list
        adr_match = re.search(
            r'<!-- flow-bash-wrapper-set:begin -->(.*?)<!-- flow-bash-wrapper-set:end -->',
            adr_text,
            re.DOTALL
        )
        if adr_match:
            wrapper_section = adr_match.group(1)
            for line in wrapper_section.split('\n'):
                line = line.strip()
                # Skip empty lines and HTML comments
                if line and not line.startswith('<') and not line.startswith('<!--'):
                    # Handle both quoted and unquoted wrappers
                    wrapper = line.strip('",\' ')
                    if wrapper:
                        adr_wrappers.add(wrapper)

    h.test_result(
        "ADR-0008 defines flow-bash-wrapper-set block",
        len(adr_wrappers) > 0,
        f"Found {len(adr_wrappers)} wrappers in ADR"
    )

    if guard_wrappers and adr_wrappers:
        # Check if they're the same (drift guard)
        missing_in_adr = guard_wrappers - adr_wrappers
        extra_in_adr = adr_wrappers - guard_wrappers

        h.test_result(
            "Guard and ADR wrapper sets are equal (drift guard)",
            guard_wrappers == adr_wrappers,
            f"Missing in ADR: {missing_in_adr}\nExtra in ADR: {extra_in_adr}"
        )

    print()
    print("[Test 10] Bash guard verb denylist consistency\n")

    # Extract PUBLISHING_VERBS from flow-bash-guard.py
    verb_match = re.search(
        r'PUBLISHING_VERBS\s*=\s*\((.*?)\)',
        bash_guard_text,
        re.DOTALL
    )

    guard_verbs = set()
    if verb_match:
        verb_str = verb_match.group(1)
        for line in verb_str.split('\n'):
            line = line.strip()
            if line.startswith('"') or line.startswith("'"):
                verb = line.strip('",\' ')
                guard_verbs.add(verb)

    h.test_result(
        "Bash guard defines PUBLISHING_VERBS",
        len(guard_verbs) > 0,
        f"Found {len(guard_verbs)} verbs"
    )

    # Verify guard blocks at least one publishing verb
    code, _ = run_guard_script(flow_bash_guard, "flow-implement", {
        "tool_name": "Bash",
        "tool_input": {"command": "git push"}
    })
    h.test_result(
        "Bash guard blocks 'git push' from verb denylist",
        code == 2
    )

    print()

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
