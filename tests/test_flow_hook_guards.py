#!/usr/bin/env python3
"""
Test suite for flow step agent hook guards.

Covers:
1. Hook guard exit codes: exit 2 on invalid payloads, 0 on allow, 2 on block
2. Agent guard: allowlist for subagent_type, both Agent and Task matchers
3. Skill guard: exactly one skill per agent, namespaced names, auto mode
4. Bash guard: wrapper forms and publishing verbs (Q1 = C), shlex parsing, auto mode
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

    print("[Test 1] Hook guard script exit codes and docstrings\n")

    flow_agent_guard = REPO_ROOT / "scripts" / "flow-agent-guard.py"
    flow_skill_guard = REPO_ROOT / "scripts" / "flow-skill-guard.py"
    flow_bash_guard = REPO_ROOT / "scripts" / "flow-bash-guard.py"

    h.test_result("flow-agent-guard.py exists", flow_agent_guard.exists())
    h.test_result("flow-skill-guard.py exists", flow_skill_guard.exists())
    h.test_result("flow-bash-guard.py exists", flow_bash_guard.exists())

    if not (flow_agent_guard.exists() and flow_skill_guard.exists() and flow_bash_guard.exists()):
        h.summarize_and_exit()

    # Verify exit code docstrings in main()
    bash_guard_text = flow_bash_guard.read_text()
    h.test_result(
        "Bash guard main() documents exit codes",
        "0: allow" in bash_guard_text and "2: block" in bash_guard_text,
        "Missing exit code docstring in bash guard"
    )

    agent_guard_text = flow_agent_guard.read_text()
    h.test_result(
        "Agent guard main() documents exit codes",
        "0: allow" in agent_guard_text and "2: block" in agent_guard_text,
        "Missing exit code docstring in agent guard"
    )

    skill_guard_text = flow_skill_guard.read_text()
    h.test_result(
        "Skill guard main() documents exit codes",
        "0: allow" in skill_guard_text and "2: block" in skill_guard_text,
        "Missing exit code docstring in skill guard"
    )

    print()
    print("[Test 2] Agent guard: invalid payloads exit 2\n")

    # Test empty payload
    code, _ = run_guard_script(flow_agent_guard, "flow-implement", {})
    h.test_result(
        "Agent guard exits 2 on empty payload",
        code == 2
    )

    # Test non-dict payload
    try:
        result = subprocess.run(
            ["python3", str(flow_agent_guard), "--agent", "flow-implement"],
            input='[]',  # Array, not dict
            capture_output=True,
            text=True,
            timeout=5
        )
        h.test_result(
            "Agent guard exits 2 on non-dict payload",
            result.returncode == 2
        )
    except Exception as e:
        h.test_result("Agent guard exits 2 on non-dict payload", False, str(e))

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

    # Test non-dict tool_input
    code, _ = run_guard_script(flow_agent_guard, "flow-implement", {
        "tool_name": "Agent",
        "tool_input": "not a dict"
    })
    h.test_result(
        "Agent guard exits 2 on non-dict tool_input",
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

    # Test empty subagent_type
    code, _ = run_guard_script(flow_agent_guard, "flow-implement", {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": ""}
    })
    h.test_result(
        "Agent guard exits 2 on empty subagent_type",
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

    # Test non-dict payload
    try:
        result = subprocess.run(
            ["python3", str(flow_skill_guard), "--agent", "flow-implement"],
            input='[]',  # Array, not dict
            capture_output=True,
            text=True,
            timeout=5
        )
        h.test_result(
            "Skill guard exits 2 on non-dict payload",
            result.returncode == 2
        )
    except Exception as e:
        h.test_result("Skill guard exits 2 on non-dict payload", False, str(e))

    code, _ = run_guard_script(flow_skill_guard, "flow-implement", {
        "tool_name": "Skill",
        "tool_input": {}
    })
    h.test_result(
        "Skill guard exits 2 on missing skill",
        code == 2
    )

    code, _ = run_guard_script(flow_skill_guard, "flow-implement", {
        "tool_name": "Skill",
        "tool_input": {"skill": ""}
    })
    h.test_result(
        "Skill guard exits 2 on empty skill",
        code == 2
    )

    code, _ = run_guard_script(flow_skill_guard, "flow-implement", {
        "tool_name": "Skill",
        "tool_input": "not a dict"
    })
    h.test_result(
        "Skill guard exits 2 on non-dict tool_input",
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
    print("[Test 5.5] Skill guard: namespaced skill names (F22)\n")

    # Test namespaced skill form (e.g., "plugin:implement-with-haiku")
    code, _ = run_guard_script(flow_skill_guard, "flow-implement", {
        "tool_name": "Skill",
        "tool_input": {"skill": "plugin:implement-with-haiku"}
    })
    h.test_result(
        "Skill guard allows namespaced skill 'plugin:implement-with-haiku' for flow-implement",
        code == 0,
        f"Expected 0, got {code}"
    )

    code, _ = run_guard_script(flow_skill_guard, "flow-review", {
        "tool_name": "Skill",
        "tool_input": {"skill": "custom:expert-review"}
    })
    h.test_result(
        "Skill guard allows namespaced skill 'custom:expert-review' for flow-review",
        code == 0,
        f"Expected 0, got {code}"
    )

    code, _ = run_guard_script(flow_skill_guard, "flow-implement", {
        "tool_name": "Skill",
        "tool_input": {"skill": "plugin:expert-review"}
    })
    h.test_result(
        "Skill guard blocks namespaced skill 'plugin:expert-review' for flow-implement",
        code == 2,
        f"Expected 2, got {code}"
    )

    print()
    print("[Test 6] Bash guard: invalid payloads exit 2 (F19)\n")

    # Test non-dict payload
    try:
        result = subprocess.run(
            ["python3", str(flow_bash_guard), "--agent", "flow-implement"],
            input='[]',  # Array, not dict
            capture_output=True,
            text=True,
            timeout=5
        )
        h.test_result(
            "Bash guard exits 2 on non-dict payload",
            result.returncode == 2
        )
    except Exception as e:
        h.test_result("Bash guard exits 2 on non-dict payload", False, str(e))

    # Test empty command
    code, _ = run_guard_script(flow_bash_guard, "flow-implement", {
        "tool_name": "Bash",
        "tool_input": {"command": ""}
    })
    h.test_result(
        "Bash guard exits 2 on empty command",
        code == 2
    )

    # Test missing command
    code, _ = run_guard_script(flow_bash_guard, "flow-implement", {
        "tool_name": "Bash",
        "tool_input": {}
    })
    h.test_result(
        "Bash guard exits 2 on missing command",
        code == 2
    )

    # Test non-string command
    code, _ = run_guard_script(flow_bash_guard, "flow-implement", {
        "tool_name": "Bash",
        "tool_input": {"command": 123}
    })
    h.test_result(
        "Bash guard exits 2 on non-string command",
        code == 2
    )

    # Test non-dict tool_input
    code, _ = run_guard_script(flow_bash_guard, "flow-implement", {
        "tool_name": "Bash",
        "tool_input": "not a dict"
    })
    h.test_result(
        "Bash guard exits 2 on non-dict tool_input",
        code == 2
    )

    print()
    print("[Test 7] Bash guard: wrapper forms exit 2 (Q1 = C, F1-F7)\n")

    wrapper_tests = [
        ("sh -c 'git push'", True, "sh -c"),
        ("bash -c 'echo hi'", True, "bash -c"),
        ("zsh -c 'ls'", True, "zsh -c"),
        ("eval 'git push'", True, "eval"),
        ("xargs git push", True, "xargs git"),
        ("xargs gh pr create", True, "xargs gh"),
        ("env X=1 git push", True, "env git"),
        ("env A=B gh issue create", True, "env gh"),
        ("git --git-dir=/tmp push", True, "git --git-dir"),
        ("git --git-dir /tmp push", True, "git --git-dir (space)"),
        ("git --exec-path=/tmp push", True, "git --exec-path"),
        ("git --exec-path /tmp push", True, "git --exec-path (space)"),
        ("git -c user.name=test push", True, "git -c"),
        ("gh api -X POST /repos/owner/repo", True, "gh api -X POST"),
        ("gh api --method DELETE /repos/owner/repo", True, "gh api --method DELETE"),
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
    print("[Test 8] Bash guard: publishing verbs exit 2 (F3, multi-word tuple matching)\n")

    publish_tests = [
        ("git push origin main", True, "git push"),
        ("git remote add upstream https://...", True, "git remote add"),
        ("git remote set-url origin https://...", True, "git remote set-url"),
        ("gh pr create --title 'test'", True, "gh pr create"),
        ("gh pr merge --squash", True, "gh pr merge"),
        ("gh pr edit --add-label bug", True, "gh pr edit"),
        ("gh pr comment --body 'hi'", True, "gh pr comment"),
        ("gh pr close", True, "gh pr close"),
        ("gh pr review --approve", True, "gh pr review"),
        ("gh issue create --title test", True, "gh issue create"),
        ("gh issue edit --title test", True, "gh issue edit"),
        ("gh issue comment --body test", True, "gh issue comment"),
        ("gh issue close", True, "gh issue close"),
        ("gh release create v1.0", True, "gh release create"),
        ("gh repo create test", True, "gh repo create"),
        ("gh repo delete test", True, "gh repo delete"),
        ("gh gist create file.txt", True, "gh gist create"),
        ("gh workflow run test.yml", True, "gh workflow run"),
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
    print("[Test 9] Bash guard: legitimate commands exit 0\n")

    legit_tests = [
        ("just check", "just check"),
        ("python3 tests/run_all.py", "python3 tests"),
        ("git add -A", "git add"),
        ("git commit -m 'test'", "git commit"),
        ("git apply patch.diff", "git apply"),
        ("git status", "git status"),
        ("git diff HEAD", "git diff"),
        ("gh api /repos/owner/repo", "gh api GET"),
        ("gh api -X GET /repos/owner/repo", "gh api -X GET"),
        ("git log --oneline", "git log"),
        ("git show HEAD", "git show"),
        ("git ls-files", "git ls-files"),
        ("git grep pattern", "git grep"),
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
    print("[Test 10] Bash guard: unbalanced quotes and parse errors block (F11)\n")

    # Unbalanced quotes should be unparseable and block (fail-closed)
    code, _ = run_guard_script(flow_bash_guard, "flow-implement", {
        "tool_name": "Bash",
        "tool_input": {"command": "git commit -m 'unbalanced"}
    })
    h.test_result(
        "Bash guard blocks unparseable command (unbalanced quote)",
        code == 2,
        f"Expected 2, got {code}"
    )

    code, _ = run_guard_script(flow_bash_guard, "flow-implement", {
        "tool_name": "Bash",
        "tool_input": {"command": 'echo "unclosed double quote'}
    })
    h.test_result(
        "Bash guard blocks unparseable command (unclosed double quote)",
        code == 2,
        f"Expected 2, got {code}"
    )

    print()
    print("[Test 11] Bash guard: boundary conditions and global options\n")

    # Test case: git -C at the end of token list
    boundary_tests = [
        ("git -C /tmp", "git -C with path", 0),
        ("git -c user.name=test", "git -c (should block)", 2),
        ("git -C /tmp add file.txt", "git -C /tmp with verb", 0),
        ("git -c key=val add file.txt", "git -c with verb (should block)", 2),
    ]

    for command, desc, expected in boundary_tests:
        code, _ = run_guard_script(flow_bash_guard, "flow-implement", {
            "tool_name": "Bash",
            "tool_input": {"command": command}
        })
        h.test_result(
            f"Bash guard: '{desc}' -> {expected}",
            code == expected,
            f"Expected {expected}, got {code}"
        )

    print()
    print("[Test 12] Bash guard drift guard: wrapper set consistency (F20)\n")

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
    print("[Test 13] Bash guard verb denylist consistency (F11, drift guard)\n")

    # Extract PUBLISHING_VERBS from flow-bash-guard.py (now as tuples)
    # Use a greedy match that captures all tuples until the closing paren
    verb_match = re.search(
        r'PUBLISHING_VERBS\s*=\s*\(((?:[^()]*|\([^)]*\))*)\)',
        bash_guard_text,
        re.DOTALL
    )

    guard_verbs = set()
    if verb_match:
        verb_str = verb_match.group(1)
        # Find all tuple patterns: ("x", "y", "z")
        tuple_pattern = re.findall(r'\(\s*(?:"[^"]+"|\'[^\']+\')+\s*(?:,\s*(?:"[^"]+"|\'[^\']+\')\s*)*\)', verb_str)
        for tuple_str in tuple_pattern:
            # Extract strings from the tuple
            strings = re.findall(r'["\']([^"\']+)["\']', tuple_str)
            if strings:
                verb = ' '.join(strings)
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
    print("[Test 14] Auto mode for all guards\n")

    # Test auto mode: non-flow agent should pass through
    code, _ = run_guard_script(flow_bash_guard, "auto", {
        "agent_type": "general-purpose",
        "tool_name": "Bash",
        "tool_input": {"command": "git reset --hard"}
    })
    h.test_result(
        "Bash guard auto mode allows non-flow agent (general-purpose)",
        code == 0,
        f"Expected 0, got {code}"
    )

    # Test auto mode: flow-implement with allowed command
    code, _ = run_guard_script(flow_bash_guard, "auto", {
        "agent_type": "flow-implement",
        "tool_name": "Bash",
        "tool_input": {"command": "git add -A"}
    })
    h.test_result(
        "Bash guard auto mode enforces flow-implement (allowed)",
        code == 0,
        f"Expected 0, got {code}"
    )

    # Test auto mode: flow-implement with forbidden command
    code, _ = run_guard_script(flow_bash_guard, "auto", {
        "agent_type": "flow-implement",
        "tool_name": "Bash",
        "tool_input": {"command": "git push"}
    })
    h.test_result(
        "Bash guard auto mode enforces flow-implement (blocked)",
        code == 2,
        f"Expected 2, got {code}"
    )

    # Test auto mode: flow-review passes through (no bash guard for flow-review)
    code, _ = run_guard_script(flow_bash_guard, "auto", {
        "agent_type": "flow-review",
        "tool_name": "Bash",
        "tool_input": {"command": "git push"}
    })
    h.test_result(
        "Bash guard auto mode allows flow-review",
        code == 0,
        f"Expected 0, got {code}"
    )

    # Test auto mode: missing agent_type passes through
    code, _ = run_guard_script(flow_bash_guard, "auto", {
        "tool_name": "Bash",
        "tool_input": {"command": "git reset --hard"}
    })
    h.test_result(
        "Bash guard auto mode allows missing agent_type",
        code == 0,
        f"Expected 0, got {code}"
    )

    # Test auto mode for agent guard
    code, _ = run_guard_script(flow_agent_guard, "auto", {
        "agent_type": "flow-implement",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "plan-implementer"}
    })
    h.test_result(
        "Agent guard auto mode enforces flow-implement",
        code == 0,
        f"Expected 0, got {code}"
    )

    code, _ = run_guard_script(flow_agent_guard, "auto", {
        "agent_type": "general-purpose",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "expert-reviewer"}
    })
    h.test_result(
        "Agent guard auto mode allows non-flow agent",
        code == 0,
        f"Expected 0, got {code}"
    )

    # Test auto mode for skill guard
    code, _ = run_guard_script(flow_skill_guard, "auto", {
        "agent_type": "flow-implement",
        "tool_name": "Skill",
        "tool_input": {"skill": "implement-with-haiku"}
    })
    h.test_result(
        "Skill guard auto mode enforces flow-implement",
        code == 0,
        f"Expected 0, got {code}"
    )

    code, _ = run_guard_script(flow_skill_guard, "auto", {
        "agent_type": "other-agent",
        "tool_name": "Skill",
        "tool_input": {"skill": "any-skill"}
    })
    h.test_result(
        "Skill guard auto mode allows non-flow agent",
        code == 0,
        f"Expected 0, got {code}"
    )

    print()

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
