#!/usr/bin/env python3
"""
Test suite for flow-plan Bash guard publish-deny profile (#360, Step 6).

Covers:
1. Publish forms are denied: git push, gh pr|issue create|edit|comment|close, gh api -X/--method
2. Known bypass is allowed: subprocess.run with obfuscated git verb
3. flow-assessor allows git log in auto mode (it's pass-through)

Run with: python3 tests/test_flow_plan_publish_deny.py
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


def run_guard_script(script_path, agent, payload_dict):
    """Run a guard script with JSON payload and return (exit_code, stderr).

    On timeout or crash, returns (None, stderr) to distinguish from deny (exit 2).
    """
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
        return None, "Timeout"
    except Exception as e:
        return None, str(e)


def main():
    h = Harness("FLOW-PLAN PUBLISH-DENY PROFILE TEST SUITE")

    flow_bash_guard = REPO_ROOT / "scripts" / "flow-bash-guard.py"

    if not flow_bash_guard.exists():
        h.test_result("flow-bash-guard.py exists", False)
        h.summarize_and_exit()

    print("[Test 1] flow-plan denies publishing forms\n")

    deny_forms = [
        ("git push origin main", "git push", True),
        ("gh pr create --title 'test'", "gh pr create", True),
        ("gh pr edit --add-label bug", "gh pr edit", True),
        ("gh pr comment --body 'hi'", "gh pr comment", True),
        ("gh pr close", "gh pr close", True),
        ("gh issue create --title test", "gh issue create", True),
        ("gh issue edit --title test", "gh issue edit", True),
        ("gh issue comment --body test", "gh issue comment", True),
        ("gh issue close", "gh issue close", True),
        ("python3 -c \"import subprocess; subprocess.run(['git', 'push'])\"", "python3 -c with git push", True),
        ("gh api -X POST /repos/owner/repo", "gh api -X POST", True),
        ("gh api --method PATCH /repos/owner/repo", "gh api --method PATCH", True),
        ("gh api -f key=value /repos/owner/repo", "gh api -f (body flag)", True),
        ("gh api -F key=value /repos/owner/repo", "gh api -F (body flag)", True),
        ("gh api --field key=value /repos/owner/repo", "gh api --field", True),
        ("gh api --input stdin /repos/owner/repo", "gh api --input", True),
    ]

    for command, desc, should_deny in deny_forms:
        code, stderr = run_guard_script(flow_bash_guard, "flow-plan", {
            "tool_name": "Bash",
            "tool_input": {"command": command}
        })
        if should_deny:
            h.test_result(
                f"flow-plan denies '{desc}'",
                code == 2,
                f"Expected 2, got {code}"
            )
            # Assert deny message appears in stderr for clarity
            if code == 2 and stderr:
                print(f"    (deny reason: {stderr.strip()})")

    print()
    print("[Test 2] flow-plan allows legitimate git/gh commands\n")

    allow_forms = [
        ("git add -A", "git add"),
        ("git commit -m 'test'", "git commit"),
        ("git log --oneline", "git log"),
        ("git status", "git status"),
        ("git diff HEAD", "git diff"),
        ("gh api /repos/owner/repo", "gh api GET"),
        ("gh api -X GET /repos/owner/repo", "gh api -X GET"),
        ("gh pr view --json state", "gh pr view"),
        ("gh issue view 123", "gh issue view"),
    ]

    for command, desc in allow_forms:
        code, _ = run_guard_script(flow_bash_guard, "flow-plan", {
            "tool_name": "Bash",
            "tool_input": {"command": command}
        })
        h.test_result(
            f"flow-plan allows '{desc}'",
            code == 0,
            f"Expected 0, got {code}"
        )

    print()
    print("[Test 3] Known bypass: obfuscated verb is allowed (documented residual)\n")

    # Documented residual: the known bypass of using string concatenation to hide the verb
    # cannot be caught by this guard and is documented as a safe fallback assumption
    # (python3 -c with arbitrary code is already a known bypass per ADR-0008).
    bypass_code, _ = run_guard_script(flow_bash_guard, "flow-plan", {
        "tool_name": "Bash",
        "tool_input": {"command": "python3 -c \"subprocess.run(['g'+'it','push'])\""}
    })

    h.test_result(
        "flow-plan allows known bypass (g+'it'+'push' obfuscation, documented residual)",
        bypass_code == 0,
        f"Expected 0 (known bypass allowed), got {bypass_code}"
    )

    print()
    print("[Test 4] flow-assessor auto mode: allows git commands (pass-through)\n")

    # flow-assessor is pass-through in bash guard auto mode
    code, _ = run_guard_script(flow_bash_guard, "auto", {
        "agent_type": "flow-assessor",
        "tool_name": "Bash",
        "tool_input": {"command": "git log --oneline -10"}
    })
    h.test_result(
        "Bash guard auto mode allows flow-assessor git log (pass-through)",
        code == 0,
        f"Expected 0, got {code}"
    )
    push_code, _ = run_guard_script(flow_bash_guard, "auto", {
        "agent_type": "flow-assessor",
        "tool_name": "Bash",
        "tool_input": {"command": "git push origin HEAD"}
    })
    h.test_result(
        "Bash guard auto mode allows flow-assessor git push (pass-through, publish-deny is flow-plan only)",
        push_code == 0,
        f"Expected 0, got {push_code}"
    )

    print()
    print("[Test 5] flow-plan with slugify.sh and ticket-title.txt commands\n")

    # These are sanctioned per #389
    slugify_code, _ = run_guard_script(flow_bash_guard, "flow-plan", {
        "tool_name": "Bash",
        "tool_input": {"command": "bash ~/.claude/scripts/slugify.sh"}
    })
    h.test_result(
        "flow-plan allows bash ~/.claude/scripts/slugify.sh",
        slugify_code == 0,
        f"Expected 0, got {slugify_code}"
    )

    # Note: ticket-title.txt is a file path, not a command, so no command test needed

    print()
    print("[Test 6] flow-plan heredoc quote-blind fallback: allowed when no publishing verb\n")

    # Regression test: heredoc with apostrophe triggers quote-blind fallback
    # This should be allowed when no publishing verb is present
    heredoc_safe_code, _ = run_guard_script(flow_bash_guard, "flow-plan", {
        "tool_name": "Bash",
        "tool_input": {"command": "cat <<'EOF'\nThis is a heredoc with an apostrophe's content\nEOF\n"}
    })
    h.test_result(
        "flow-plan allows heredoc with apostrophe when no publishing verb",
        heredoc_safe_code == 0,
        f"Expected 0, got {heredoc_safe_code}"
    )

    # But deny if heredoc contains a publishing verb
    heredoc_deny_code, _ = run_guard_script(flow_bash_guard, "flow-plan", {
        "tool_name": "Bash",
        "tool_input": {"command": "cat <<'EOF'\ngit push origin main\nEOF\n"}
    })
    h.test_result(
        "flow-plan denies heredoc containing git push (quote-blind scan)",
        heredoc_deny_code == 2,
        f"Expected 2, got {heredoc_deny_code}"
    )

    print()
    print("[Test 7] flow-plan nesting limit denial\n")

    # Nesting deeper than MAX_NESTING should be denied (always, all agents)
    # Create a deeply nested command: $( ... $(
    deep_nesting = "$( " * 70 + "git log" + " )" * 70
    nesting_code, _ = run_guard_script(flow_bash_guard, "flow-plan", {
        "tool_name": "Bash",
        "tool_input": {"command": deep_nesting}
    })
    h.test_result(
        "flow-plan denies nesting deeper than MAX_NESTING",
        nesting_code == 2,
        f"Expected 2, got {nesting_code}"
    )

    # Pin the exact boundary: depth MAX_NESTING (64) is allowed, 65 is denied
    at_limit = "$( " * 64 + "git log" + " )" * 64
    over_limit = "$( " * 65 + "git log" + " )" * 65
    at_code, _ = run_guard_script(flow_bash_guard, "flow-plan", {
        "tool_name": "Bash",
        "tool_input": {"command": at_limit}
    })
    over_code, _ = run_guard_script(flow_bash_guard, "flow-plan", {
        "tool_name": "Bash",
        "tool_input": {"command": over_limit}
    })
    h.test_result(
        "flow-plan nesting boundary: 65 levels denied, 64 levels not denied for nesting",
        over_code == 2 and at_code != 2,
        f"Expected over=2 and at!=2, got over={over_code}, at={at_code}"
    )

    print()

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
