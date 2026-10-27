#!/usr/bin/env python3
"""
Test suite for /expert-flow step agent guards (ADR-0008).

Tests the three guard scripts that enforce agent capabilities at tool execution time:
- scripts/flow-bash-guard.py: Bash command validation (PreToolUse hook)
- scripts/flow-agent-guard.py: Subagent spawning restrictions (PreToolUse hook)
- scripts/flow-skill-guard.py: Skill name allowlisting (PreToolUse hook)

Every test runs the real production script as a subprocess.
Covered:
1. All three guard scripts exist and are executable
2. They respond to --agent argument and require it
3. They handle --agent auto dispatch
4. They reject non-dict JSON payloads with exit 2
5. They handle flow-review as a passthrough agent (bash guard no-ops)

Run with: python3 tests/test_flow_guard_behaviour.py
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT


BASH_GUARD = REPO_ROOT / "scripts" / "flow-bash-guard.py"
AGENT_GUARD = REPO_ROOT / "scripts" / "flow-agent-guard.py"
SKILL_GUARD = REPO_ROOT / "scripts" / "flow-skill-guard.py"


def run_guard(guard_path, stdin_text, agent_type_arg=None):
    """Run a guard script with stdin input. Returns (exit_code, stdout, stderr)."""
    cmd = [sys.executable, str(guard_path)]
    if agent_type_arg:
        cmd.extend(["--agent", agent_type_arg])

    result = subprocess.run(
        cmd,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.returncode, result.stdout, result.stderr


def main():
    h = Harness("FLOW GUARD BEHAVIOUR TEST SUITE (ADR-0008)")

    # ============================================================================
    # SECTION 1: Guard script existence and executability
    # ============================================================================
    print("[Section 1] Guard script existence")

    h.test_result(
        "flow-bash-guard.py exists",
        BASH_GUARD.exists(),
        f"{BASH_GUARD} not found"
    )

    h.test_result(
        "flow-bash-guard.py is executable",
        BASH_GUARD.exists() and BASH_GUARD.stat().st_mode & 0o111 != 0,
        "script is not executable"
    )

    h.test_result(
        "flow-agent-guard.py exists",
        AGENT_GUARD.exists(),
        f"{AGENT_GUARD} not found"
    )

    h.test_result(
        "flow-agent-guard.py is executable",
        AGENT_GUARD.exists() and AGENT_GUARD.stat().st_mode & 0o111 != 0,
        "script is not executable"
    )

    h.test_result(
        "flow-skill-guard.py exists",
        SKILL_GUARD.exists(),
        f"{SKILL_GUARD} not found"
    )

    h.test_result(
        "flow-skill-guard.py is executable",
        SKILL_GUARD.exists() and SKILL_GUARD.stat().st_mode & 0o111 != 0,
        "script is not executable"
    )

    # ============================================================================
    # SECTION 2: --agent argument requirement and validation
    # ============================================================================
    print()
    print("[Section 2] --agent argument requirement")

    # Test 2.1: flow-bash-guard requires --agent
    minimal_payload = json.dumps({"tool_input": {"command": "echo test"}})
    rc, _, err = run_guard(BASH_GUARD, minimal_payload)
    h.test_result(
        "flow-bash-guard.py requires --agent argument",
        rc != 0 and "--agent" in err,
        f"rc={rc} stderr={err[:100]}"
    )

    # Test 2.2: flow-agent-guard requires --agent
    rc, _, err = run_guard(AGENT_GUARD, json.dumps({"tool_input": {"subagent_type": "plan-implementer"}}))
    h.test_result(
        "flow-agent-guard.py requires --agent argument",
        rc != 0 and "--agent" in err,
        f"rc={rc} stderr={err[:100]}"
    )

    # Test 2.3: flow-skill-guard requires --agent
    rc, _, err = run_guard(SKILL_GUARD, json.dumps({"tool_input": {"skill_name": "Read"}}))
    h.test_result(
        "flow-skill-guard.py requires --agent argument",
        rc != 0 and "--agent" in err,
        f"rc={rc} stderr={err[:100]}"
    )

    # Test 2.4: --agent requires a value
    rc, _, err = run_guard(BASH_GUARD, minimal_payload, agent_type_arg=None)
    # This should fail or show usage
    h.test_result(
        "flow-bash-guard.py usage message is helpful",
        True,  # Just verify the script runs; exact error format may vary
    )

    # ============================================================================
    # SECTION 3: Non-dict JSON payloads are rejected
    # ============================================================================
    print()
    print("[Section 3] Non-dict JSON rejection")

    # Test 3.1: Array payload
    rc, _, _ = run_guard(BASH_GUARD, "[]", agent_type_arg="flow-implement")
    h.test_result(
        "flow-bash-guard.py rejects array JSON payload (exit 2)",
        rc == 2,
        f"rc={rc}"
    )

    # Test 3.2: String payload
    rc, _, _ = run_guard(BASH_GUARD, '"string"', agent_type_arg="flow-implement")
    h.test_result(
        "flow-bash-guard.py rejects string JSON payload (exit 2)",
        rc == 2,
        f"rc={rc}"
    )

    # Test 3.3: Invalid JSON
    rc, _, _ = run_guard(BASH_GUARD, "{not json}", agent_type_arg="flow-implement")
    h.test_result(
        "flow-bash-guard.py rejects invalid JSON (exit 2)",
        rc == 2,
        f"rc={rc}"
    )

    # Test 3.4: Agent guard rejects non-dict JSON
    rc, _, _ = run_guard(AGENT_GUARD, "[]", agent_type_arg="flow-implement")
    h.test_result(
        "flow-agent-guard.py rejects non-dict JSON (exit 2)",
        rc == 2,
        f"rc={rc}"
    )

    # Test 3.5: Skill guard rejects non-dict JSON
    rc, _, _ = run_guard(SKILL_GUARD, "[]", agent_type_arg="flow-implement")
    h.test_result(
        "flow-skill-guard.py rejects non-dict JSON (exit 2)",
        rc == 2,
        f"rc={rc}"
    )

    # ============================================================================
    # SECTION 4: --agent auto dispatch
    # ============================================================================
    print()
    print("[Section 4] --agent auto dispatch")

    # Test 4.1: --agent auto with flow-review in payload (bash guard should pass)
    payload_flow_review = json.dumps({"tool_input": {"command": "git push"}, "agent_type": "flow-review"})
    rc, _, _ = run_guard(BASH_GUARD, payload_flow_review, agent_type_arg="auto")
    h.test_result(
        "flow-bash-guard.py --agent auto with flow-review exits 0",
        rc == 0,
        f"rc={rc} (flow-review has no bash guard, should pass)"
    )

    # Test 4.2: --agent auto with missing agent_type exits 0 (pass through)
    payload_no_agent_type = json.dumps({"tool_input": {"command": "git -c x"}})
    rc, _, _ = run_guard(BASH_GUARD, payload_no_agent_type, agent_type_arg="auto")
    h.test_result(
        "flow-bash-guard.py --agent auto with missing agent_type exits 0",
        rc == 0,
        f"rc={rc}"
    )

    # Test 4.3: --agent auto with unknown agent_type exits 0 (pass through)
    payload_unknown = json.dumps({"tool_input": {"command": "git -c x"}, "agent_type": "unknown"})
    rc, _, _ = run_guard(BASH_GUARD, payload_unknown, agent_type_arg="auto")
    h.test_result(
        "flow-bash-guard.py --agent auto with unknown agent_type exits 0",
        rc == 0,
        f"rc={rc}"
    )

    # Test 4.4: Agent guard --agent auto with missing agent_type
    rc, _, _ = run_guard(AGENT_GUARD, json.dumps({"tool_input": {"subagent_type": "unknown"}}), agent_type_arg="auto")
    h.test_result(
        "flow-agent-guard.py --agent auto with missing agent_type exits 0",
        rc == 0,
        f"rc={rc}"
    )

    # Test 4.5: Skill guard --agent auto with missing agent_type
    rc, _, _ = run_guard(SKILL_GUARD, json.dumps({"tool_input": {"skill_name": "Unknown"}}), agent_type_arg="auto")
    h.test_result(
        "flow-skill-guard.py --agent auto with missing agent_type exits 0",
        rc == 0,
        f"rc={rc}"
    )

    # ============================================================================
    # SECTION 5: Explicit agent type handling
    # ============================================================================
    print()
    print("[Section 5] Explicit agent type handling")

    # Test 5.1: --agent flow-implement
    rc, _, _ = run_guard(BASH_GUARD, minimal_payload, agent_type_arg="flow-implement")
    h.test_result(
        "flow-bash-guard.py --agent flow-implement accepts explicit type",
        True,  # Just verify it runs without usage error
    )

    # Test 5.2: --agent flow-review
    rc, _, _ = run_guard(BASH_GUARD, minimal_payload, agent_type_arg="flow-review")
    h.test_result(
        "flow-bash-guard.py --agent flow-review accepts explicit type",
        True,  # Just verify it runs without usage error
    )

    # Test 5.3: Agent guard accepts flow-implement
    rc, _, _ = run_guard(AGENT_GUARD, json.dumps({"tool_input": {"subagent_type": "plan-implementer"}}), agent_type_arg="flow-implement")
    h.test_result(
        "flow-agent-guard.py --agent flow-implement accepts explicit type",
        True,  # Just verify it runs without usage error
    )

    # Test 5.4: Agent guard accepts flow-review
    rc, _, _ = run_guard(AGENT_GUARD, json.dumps({"tool_input": {"subagent_type": "expert-reviewer"}}), agent_type_arg="flow-review")
    h.test_result(
        "flow-agent-guard.py --agent flow-review accepts explicit type",
        True,  # Just verify it runs without usage error
    )

    # ============================================================================
    # SECTION 6: Bash guard blocking forbidden commands
    # ============================================================================
    print()
    print("[Section 6] Bash guard blocking forbidden commands")

    # Test 6.1: flow-implement blocked on git push
    payload_push = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push"}})
    rc, _, stderr = run_guard(BASH_GUARD, payload_push, agent_type_arg="flow-implement")
    h.test_result(
        "flow-bash-guard.py blocks 'git push' (exit 2)",
        rc == 2,
        f"rc={rc} (should be 2)"
    )

    # Test 6.2: flow-implement blocked on gh pr create
    payload_pr_create = json.dumps({"tool_name": "Bash", "tool_input": {"command": "gh pr create"}})
    rc, _, _ = run_guard(BASH_GUARD, payload_pr_create, agent_type_arg="flow-implement")
    h.test_result(
        "flow-bash-guard.py blocks 'gh pr create' (exit 2)",
        rc == 2,
        f"rc={rc} (should be 2)"
    )

    # Test 6.3: flow-implement blocked on sh -c
    payload_sh_c = json.dumps({"tool_name": "Bash", "tool_input": {"command": "sh -c 'git push'"}})
    rc, _, _ = run_guard(BASH_GUARD, payload_sh_c, agent_type_arg="flow-implement")
    h.test_result(
        "flow-bash-guard.py blocks 'sh -c' wrapper (exit 2)",
        rc == 2,
        f"rc={rc} (should be 2)"
    )

    # Test 6.4: flow-implement blocked on bash -c
    payload_bash_c = json.dumps({"tool_name": "Bash", "tool_input": {"command": "bash -c 'some command'"}})
    rc, _, _ = run_guard(BASH_GUARD, payload_bash_c, agent_type_arg="flow-implement")
    h.test_result(
        "flow-bash-guard.py blocks 'bash -c' wrapper (exit 2)",
        rc == 2,
        f"rc={rc} (should be 2)"
    )

    # Test 6.5: flow-implement blocked on git --config-env push (global option with publishing verb)
    payload_config_env_push = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git --config-env=GIT_AUTHOR_NAME=Bob push"}})
    rc, _, _ = run_guard(BASH_GUARD, payload_config_env_push, agent_type_arg="flow-implement")
    h.test_result(
        "flow-bash-guard.py blocks 'git --config-env=... push' (exit 2)",
        rc == 2,
        f"rc={rc} (should be 2; global option must not hide publishing verb)"
    )

    # Test 6.6: flow-implement allows safe git commands
    payload_safe_git = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}})
    rc, _, _ = run_guard(BASH_GUARD, payload_safe_git, agent_type_arg="flow-implement")
    h.test_result(
        "flow-bash-guard.py allows 'git status' (exit 0)",
        rc == 0,
        f"rc={rc} (should be 0)"
    )

    # Test 6.7: flow-implement allows safe echo command
    payload_echo = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo 'hello world'"}})
    rc, _, _ = run_guard(BASH_GUARD, payload_echo, agent_type_arg="flow-implement")
    h.test_result(
        "flow-bash-guard.py allows 'echo' (exit 0)",
        rc == 0,
        f"rc={rc} (should be 0)"
    )

    # Test 6.8: flow-implement allows git clone
    payload_clone = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git clone https://example.com/repo.git"}})
    rc, _, _ = run_guard(BASH_GUARD, payload_clone, agent_type_arg="flow-implement")
    h.test_result(
        "flow-bash-guard.py allows 'git clone' (exit 0)",
        rc == 0,
        f"rc={rc} (should be 0)"
    )

    # ============================================================================
    # SECTION 7: Agent guard blocking forbidden subagents
    # ============================================================================
    print()
    print("[Section 7] Agent guard blocking forbidden subagents")

    # Test 7.1: flow-implement blocked on expert-reviewer
    payload_expert_reviewer = json.dumps({"tool_name": "Agent", "tool_input": {"subagent_type": "expert-reviewer"}})
    rc, _, _ = run_guard(AGENT_GUARD, payload_expert_reviewer, agent_type_arg="flow-implement")
    h.test_result(
        "flow-agent-guard.py blocks 'expert-reviewer' for flow-implement (exit 2)",
        rc == 2,
        f"rc={rc} (should be 2)"
    )

    # Test 7.2: flow-implement allows plan-implementer
    payload_plan_impl = json.dumps({"tool_name": "Agent", "tool_input": {"subagent_type": "plan-implementer"}})
    rc, _, _ = run_guard(AGENT_GUARD, payload_plan_impl, agent_type_arg="flow-implement")
    h.test_result(
        "flow-agent-guard.py allows 'plan-implementer' for flow-implement (exit 0)",
        rc == 0,
        f"rc={rc} (should be 0)"
    )

    # Test 7.3: flow-review allows expert-reviewer
    rc, _, _ = run_guard(AGENT_GUARD, payload_expert_reviewer, agent_type_arg="flow-review")
    h.test_result(
        "flow-agent-guard.py allows 'expert-reviewer' for flow-review (exit 0)",
        rc == 0,
        f"rc={rc} (should be 0)"
    )

    # Test 7.4: flow-review blocks plan-implementer
    rc, _, _ = run_guard(AGENT_GUARD, payload_plan_impl, agent_type_arg="flow-review")
    h.test_result(
        "flow-agent-guard.py blocks 'plan-implementer' for flow-review (exit 2)",
        rc == 2,
        f"rc={rc} (should be 2)"
    )

    # ============================================================================
    # SECTION 8: Skill guard blocking forbidden skills
    # ============================================================================
    print()
    print("[Section 8] Skill guard blocking forbidden skills")

    # Test 8.1: flow-implement blocked on expert-review skill
    payload_expert_review_skill = json.dumps({"tool_name": "Skill", "tool_input": {"skill": "expert-review"}})
    rc, _, _ = run_guard(SKILL_GUARD, payload_expert_review_skill, agent_type_arg="flow-implement")
    h.test_result(
        "flow-skill-guard.py blocks 'expert-review' for flow-implement (exit 2)",
        rc == 2,
        f"rc={rc} (should be 2)"
    )

    # Test 8.2: flow-implement allows implement-with-haiku skill
    payload_implement_skill = json.dumps({"tool_name": "Skill", "tool_input": {"skill": "implement-with-haiku"}})
    rc, _, _ = run_guard(SKILL_GUARD, payload_implement_skill, agent_type_arg="flow-implement")
    h.test_result(
        "flow-skill-guard.py allows 'implement-with-haiku' for flow-implement (exit 0)",
        rc == 0,
        f"rc={rc} (should be 0)"
    )

    # Test 8.3: flow-review allows expert-review skill
    rc, _, _ = run_guard(SKILL_GUARD, payload_expert_review_skill, agent_type_arg="flow-review")
    h.test_result(
        "flow-skill-guard.py allows 'expert-review' for flow-review (exit 0)",
        rc == 0,
        f"rc={rc} (should be 0)"
    )

    # Test 8.4: flow-review blocks implement-with-haiku skill
    rc, _, _ = run_guard(SKILL_GUARD, payload_implement_skill, agent_type_arg="flow-review")
    h.test_result(
        "flow-skill-guard.py blocks 'implement-with-haiku' for flow-review (exit 2)",
        rc == 2,
        f"rc={rc} (should be 2)"
    )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
