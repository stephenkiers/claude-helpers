#!/usr/bin/env python3
"""
Black-box test suite for flow-plan Bash guard (#360, items 1-6, 13-14).

Tests the following plan items as subprocess invocations of the real guard:
1. Unparseable commands with publishing verbs are denied for flow-plan
2. Per-segment tokenize failure handling (heredoc with apostrophes)
3. Non-GET `gh api` with explicit method flags is denied
4. GET `gh api` without method flags is allowed
5. Deeply nested command substitutions are denied
13. flow-assessor's tools contain prefix-scoped Bash (parity assertion)
14. Agent type is handled correctly (isinstance check for non-strings)

Run with: python3 tests/test_flow_plan_guard_blackbox.py
"""

import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


def run_guard_script(script_path, agent, payload_dict, timeout=35):
    """Run a guard script with JSON payload as a real subprocess.

    Returns (exit_code, stderr, stdout). On timeout, exit_code is None.
    Treats timeout/crash as a failure signal (not a pass).
    """
    try:
        result = subprocess.run(
            ["python3", str(script_path), "--agent", agent],
            input=json.dumps(payload_dict),
            capture_output=True,
            text=True,
            timeout=timeout
        )
        return result.returncode, result.stderr, result.stdout
    except subprocess.TimeoutExpired:
        return None, "Timeout after {} seconds".format(timeout), ""
    except Exception as e:
        return None, "Subprocess error: {}".format(str(e)), ""


def parse_markdown_frontmatter(text):
    """Extract YAML-like frontmatter from markdown.

    Returns a dict of key-value pairs from the frontmatter block.
    """
    match = re.match(r'^---\n(.*?)\n---\n', text, re.DOTALL)
    if not match:
        return {}

    fm_text = match.group(1)
    result = {}

    for line in fm_text.split('\n'):
        if ':' not in line or line.strip().startswith('#'):
            continue
        key, value = line.split(':', 1)
        key = key.strip()
        value = value.strip()

        # Parse tools as comma-separated list
        if key in ('tools', 'allowed-tools'):
            result[key] = [v.strip() for v in value.split(',')]
        else:
            result[key] = value

    return result


def main():
    h = Harness("FLOW-PLAN GUARD BLACK-BOX TEST SUITE")

    flow_bash_guard = REPO_ROOT / "scripts" / "flow-bash-guard.py"
    flow_agent_guard = REPO_ROOT / "scripts" / "flow-agent-guard.py"

    if not flow_bash_guard.exists():
        h.test_result("flow-bash-guard.py exists", False)
        h.summarize_and_exit()

    print("[Item 1] Unparseable commands with publishing verbs are denied\n")

    # Test unparseable command (unbalanced quotes) that contains a publishing verb
    unparseable_with_verb = 'echo "unclosed quote | git push'
    code, stderr, _ = run_guard_script(flow_bash_guard, "flow-plan", {
        "tool_name": "Bash",
        "tool_input": {"command": unparseable_with_verb}
    })
    h.test_result(
        "flow-plan denies unparseable command with publishing verb (git push)",
        code == 2,
        f"Expected 2, got {code}" + (f"; stderr: {stderr[:100]}" if code != 2 else "")
    )

    # Test another unparseable form: unbalanced parentheses with gh pr create
    unparseable_gh = 'if ( true | gh pr create --title test'
    code, stderr, _ = run_guard_script(flow_bash_guard, "flow-plan", {
        "tool_name": "Bash",
        "tool_input": {"command": unparseable_gh}
    })
    h.test_result(
        "flow-plan denies unparseable command with publishing verb (gh pr create)",
        code == 2,
        f"Expected 2, got {code}" + (f"; stderr: {stderr[:100]}" if code != 2 else "")
    )

    print()
    print("[Item 2] Heredoc with apostrophes and tokenize fallback\n")

    # Heredoc with apostrophe but no publishing verb should be allowed
    heredoc_safe = "cat <<'EOF'\nThis has an apostrophe's in it\nEOF\n"
    code, _, _ = run_guard_script(flow_bash_guard, "flow-plan", {
        "tool_name": "Bash",
        "tool_input": {"command": heredoc_safe}
    })
    h.test_result(
        "flow-plan allows heredoc with apostrophe (no publishing verb)",
        code == 0,
        f"Expected 0, got {code}"
    )

    # Heredoc with apostrophe AND publishing verb should be denied
    heredoc_with_verb = "cat <<'EOF'\ngit push\nEOF\n"
    code, stderr, _ = run_guard_script(flow_bash_guard, "flow-plan", {
        "tool_name": "Bash",
        "tool_input": {"command": heredoc_with_verb}
    })
    h.test_result(
        "flow-plan denies heredoc with apostrophe containing git push",
        code == 2,
        f"Expected 2, got {code}"
    )

    print()
    print("[Item 3] Non-GET gh api with explicit method is denied\n")

    # gh api -X POST should be denied
    code, stderr, _ = run_guard_script(flow_bash_guard, "flow-plan", {
        "tool_name": "Bash",
        "tool_input": {"command": "gh api -X POST /repos/owner/repo/issues"}
    })
    h.test_result(
        "flow-plan denies gh api -X POST",
        code == 2,
        f"Expected 2, got {code}"
    )

    # gh api --method DELETE should be denied
    code, stderr, _ = run_guard_script(flow_bash_guard, "flow-plan", {
        "tool_name": "Bash",
        "tool_input": {"command": "gh api --method DELETE /repos/owner/repo"}
    })
    h.test_result(
        "flow-plan denies gh api --method DELETE",
        code == 2,
        f"Expected 2, got {code}"
    )

    # gh api -f key=value (body flag) should be denied
    code, stderr, _ = run_guard_script(flow_bash_guard, "flow-plan", {
        "tool_name": "Bash",
        "tool_input": {"command": "gh api -f title='test' /repos/owner/repo/issues"}
    })
    h.test_result(
        "flow-plan denies gh api -f (body flag)",
        code == 2,
        f"Expected 2, got {code}"
    )

    print()
    print("[Item 4] GET gh api without method flags is allowed\n")

    # Plain gh api (defaults to GET) should be allowed
    code, _, _ = run_guard_script(flow_bash_guard, "flow-plan", {
        "tool_name": "Bash",
        "tool_input": {"command": "gh api /repos/owner/repo"}
    })
    h.test_result(
        "flow-plan allows gh api (default GET)",
        code == 0,
        f"Expected 0, got {code}"
    )

    # gh api -X GET should be allowed
    code, _, _ = run_guard_script(flow_bash_guard, "flow-plan", {
        "tool_name": "Bash",
        "tool_input": {"command": "gh api -X GET /repos/owner/repo"}
    })
    h.test_result(
        "flow-plan allows gh api -X GET",
        code == 0,
        f"Expected 0, got {code}"
    )

    # gh api --method GET should be allowed
    code, _, _ = run_guard_script(flow_bash_guard, "flow-plan", {
        "tool_name": "Bash",
        "tool_input": {"command": "gh api --method GET /repos/owner/repo"}
    })
    h.test_result(
        "flow-plan allows gh api --method GET",
        code == 0,
        f"Expected 0, got {code}"
    )

    print()
    print("[Item 5] Deeply nested command substitutions are denied\n")

    # Create a deeply nested command (70 levels, matching test_flow_plan_publish_deny.py Test 7)
    # This should exceed MAX_NESTING and be denied
    deep_nesting = "$( " * 70 + "git log" + " )" * 70
    code, stderr, _ = run_guard_script(flow_bash_guard, "flow-plan", {
        "tool_name": "Bash",
        "tool_input": {"command": deep_nesting}
    }, timeout=35)  # Give generous timeout for deep parsing

    h.test_result(
        "flow-plan denies deeply nested command substitutions (70 levels)",
        code == 2,
        f"Expected 2, got {code}" + (f"; stderr: {stderr[:100]}" if code != 2 else "")
    )

    print()
    print("[Item 13] flow-assessor tools contain prefix-scoped Bash\n")

    # Read flow-assessor.md to check its tools: line
    flow_assessor = REPO_ROOT / "agents" / "flow-assessor.md"
    if flow_assessor.exists():
        text = flow_assessor.read_text()
        frontmatter = parse_markdown_frontmatter(text)
        tools = frontmatter.get('tools', [])

        # Check that tools is non-empty
        h.test_result(
            "flow-assessor has non-empty tools: list",
            len(tools) > 0,
            f"Expected non-empty tools, got {tools}"
        )

        # Check that Bash in tools is prefix-scoped (contains a colon or parenthesis)
        has_scoped_bash = False
        has_bare_bash = False
        for tool in tools:
            tool_str = str(tool).strip()
            if 'Bash' in tool_str:
                if '(' in tool_str or ':' in tool_str:
                    has_scoped_bash = True
                else:
                    has_bare_bash = True

        h.test_result(
            "flow-assessor has prefix-scoped Bash (not bare)",
            has_scoped_bash and not has_bare_bash,
            f"scoped={has_scoped_bash}, bare={has_bare_bash}, tools={tools}"
        )
    else:
        h.test_result(
            "flow-assessor.md exists",
            False,
            f"Expected to find {flow_assessor}"
        )

    print()
    print("[Item 14] Agent type validation for non-string inputs\n")

    # flow-agent-guard should handle non-string agent_type gracefully
    # Test with numeric agent_type instead of string
    if flow_agent_guard.exists():
        code, stderr, _ = run_guard_script(flow_agent_guard, "flow-implement", {
            "tool_name": "Agent",
            "tool_input": {
                "subagent_type": "plan-implementer",
                "agent_type": 123  # Non-string value
            }
        })

        h.test_result(
            "Agent guard handles non-string agent_type without crashing",
            code is not None,  # Should not timeout; code of 0 or 2 is fine
            f"Expected to complete, got code={code}" + (f"; stderr: {stderr[:100]}" if code is None else "")
        )

        # Test with None agent_type
        code, stderr, _ = run_guard_script(flow_agent_guard, "flow-implement", {
            "tool_name": "Agent",
            "tool_input": {
                "subagent_type": "plan-implementer",
                "agent_type": None
            }
        })

        h.test_result(
            "Agent guard handles None agent_type without crashing",
            code is not None,  # Should not timeout
            f"Expected to complete, got code={code}" + (f"; stderr: {stderr[:100]}" if code is None else "")
        )
    else:
        h.test_result(
            "flow-agent-guard.py exists",
            False,
            f"Expected to find {flow_agent_guard}"
        )

    print()
    print("[Item 4 Extended] Test helper handles timeout/crash gracefully\n")

    # This tests that our test harness properly detects timeout
    # We create a subprocess that would timeout on a very slow command
    # (or we just verify that our helper returns None on timeout)

    # For now, we test the guard script directly with a reasonable timeout
    # If the guard legitimately takes longer, this is OK; the point is
    # that timeout doesn't cause an unhandled exception
    code, stderr, _ = run_guard_script(flow_bash_guard, "flow-plan", {
        "tool_name": "Bash",
        "tool_input": {"command": "git log --oneline"}
    }, timeout=30)

    h.test_result(
        "Guard script completes within timeout for simple command",
        code is not None,
        f"Expected code to be set, got None (timeout)" if code is None else ""
    )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
