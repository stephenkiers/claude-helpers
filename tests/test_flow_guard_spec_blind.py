#!/usr/bin/env python3
"""
Spec-blind test suite for scripts/flow-bash-guard.py covering action plan N1-N4.

Tests written ONLY from the action plan specification without reading
the implementation. Focus areas:

N1: Shared subcommand finder - locate git/gh subcommands after global options
    - git -C <dir> push should be detected and denied
    - gh -R repo pr merge should be detected and denied
    - Global options like --no-pager, --config-env should not hide publishing verbs

N2: flow-merge denies gh pr merge when merge-queue config exists
    - With merge-queue.json present, gh pr merge is denied for flow-merge
    - Without merge-queue.json, gh pr merge --squash is allowed for flow-merge

N3: Escalation text in guard denials (N3 from action plan)
    - Denied push-forms include escalation/Ask block instruction
    - Denied gh pr merge forms include escalation text

Every test runs the real production guard as a subprocess.

Run with: python3 tests/test_flow_guard_spec_blind.py
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT


BASH_GUARD = REPO_ROOT / "scripts" / "flow-bash-guard.py"


def run_guard(agent, command):
    """Run the bash guard with a command string. Returns (exit_code, stderr)."""
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    try:
        result = subprocess.run(
            [sys.executable, str(BASH_GUARD), "--agent", agent],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=60
        )
        return result.returncode, result.stderr
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except Exception as e:
        return 2, str(e)


def main():
    h = Harness("FLOW BASH GUARD SPEC-BLIND TEST SUITE (Action Plan N1-N4)")

    print("[Test Group 1] N1: git global options should not hide publishing verbs\n")

    # Test that git -C <dir> push is properly detected and denied
    # (from action plan: "git -C <wt> push bypasses push-form allow-list")
    code, err = run_guard("flow-ship", "git -C /tmp push")
    h.test_result(
        "git -C /tmp push is detected and denied (not at position 1)",
        code == 2,
        f"exit={code} (should find push at position 2, after -C)"
    )

    # Test git -C with multiple options
    code, err = run_guard("flow-ship", "git -C /tmp --no-pager push")
    h.test_result(
        "git -C /tmp --no-pager push is denied",
        code == 2,
        f"exit={code}"
    )

    # Test git --no-pager push
    code, err = run_guard("flow-ship", "git --no-pager push")
    h.test_result(
        "git --no-pager push is detected and denied (not at position 1)",
        code == 2,
        f"exit={code}"
    )

    # Test git --config-env with equals sign
    code, err = run_guard("flow-ship", "git --config-env=GIT_AUTHOR_NAME=Bob push")
    h.test_result(
        "git --config-env=... push is detected and denied",
        code == 2,
        f"exit={code}"
    )

    # Test git -p (short form of --paginate)
    code, err = run_guard("flow-ship", "git -p push")
    h.test_result(
        "git -p push is detected and denied",
        code == 2,
        f"exit={code}"
    )

    # Happy path: git -C with non-publishing verb should be allowed
    code, err = run_guard("flow-ship", "git -C /tmp status")
    h.test_result(
        "git -C /tmp status is allowed (not a publishing verb)",
        code == 0,
        f"exit={code}"
    )

    # Happy path: git --no-pager with non-publishing verb
    code, err = run_guard("flow-ship", "git --no-pager log --oneline")
    h.test_result(
        "git --no-pager log is allowed",
        code == 0,
        f"exit={code}"
    )

    print()
    print("[Test Group 2] N1: gh global options should not hide publishing verbs\n")

    # Test gh -R repo pr merge (from action plan: gh -R gap for flow-ship)
    # For flow-ship, gh pr merge is always denied
    code, err = run_guard("flow-ship", "gh -R myrepo pr merge")
    h.test_result(
        "gh -R repo pr merge is detected and denied for flow-ship",
        code == 2,
        f"exit={code}"
    )

    # Test gh -R repo pr create (publishing verb)
    code, err = run_guard("flow-implement", "gh -R myrepo pr create --title test")
    h.test_result(
        "gh -R repo pr create is detected and denied for flow-implement",
        code == 2,
        f"exit={code}"
    )

    # Test gh --repo (long form)
    code, err = run_guard("flow-implement", "gh --repo myrepo pr create --title test")
    h.test_result(
        "gh --repo repo pr create is detected and denied",
        code == 2,
        f"exit={code}"
    )

    # Happy path: gh -R with non-publishing verb
    code, err = run_guard("flow-implement", "gh -R myrepo issue view 123")
    h.test_result(
        "gh -R repo issue view is allowed (not a publishing verb)",
        code == 0,
        f"exit={code}"
    )

    print()
    print("[Test Group 3] N2: flow-merge gh pr merge behavior (merge-queue config dependent)\n")

    # Check if merge-queue.json exists
    merge_queue_path = REPO_ROOT.parent.parent / "merge-queue.json"
    has_merge_queue = merge_queue_path.exists()

    if has_merge_queue:
        print(f"merge-queue.json found at {merge_queue_path}")
        # When merge-queue exists, flow-merge must deny gh pr merge entirely
        code, err = run_guard("flow-merge", "gh pr merge --squash")
        h.test_result(
            "flow-merge denies gh pr merge --squash when merge-queue exists",
            code == 2,
            f"exit={code}"
        )

        code, err = run_guard("flow-merge", "gh pr merge --admin")
        h.test_result(
            "flow-merge denies gh pr merge --admin when merge-queue exists",
            code == 2,
            f"exit={code}"
        )
    else:
        print(f"merge-queue.json NOT found at {merge_queue_path}")
        # When merge-queue doesn't exist, flow-merge allows gh pr merge --squash
        code, err = run_guard("flow-merge", "gh pr merge --squash")
        h.test_result(
            "flow-merge allows gh pr merge --squash when no merge-queue config",
            code == 0,
            f"exit={code}"
        )

        # But gh pr merge without proper flags or with --admin should still be denied
        code, err = run_guard("flow-merge", "gh pr merge --admin")
        h.test_result(
            "flow-merge denies gh pr merge --admin (even without merge-queue)",
            code == 2,
            f"exit={code}"
        )

    print()
    print("[Test Group 4] N3: Escalation text in guard denials\n")

    # Test that denied push-forms include escalation instruction
    code, err = run_guard("flow-ship", "git -C /tmp push")
    h.test_result(
        "Denied git push includes 'Ask block' escalation instruction",
        code == 2 and "Ask block" in err,
        f"exit={code}, err contains 'Ask block': {'Ask block' in err}"
    )

    # Test that the denial includes a rule ID
    h.test_result(
        "Denied git push includes rule ID in error message",
        code == 2 and "rule" in err,
        f"exit={code}, err contains 'rule': {'rule' in err}"
    )

    # Test that denied gh pr merge includes escalation text
    code, err = run_guard("flow-ship", "gh pr merge")
    h.test_result(
        "Denied gh pr merge includes 'Ask block' escalation instruction",
        code == 2 and "Ask block" in err,
        f"exit={code}, err contains 'Ask block': {'Ask block' in err}"
    )

    print()
    print("[Test Group 5] Edge cases: multiple global options in sequence\n")

    # Test git with multiple global options in different forms
    code, err = run_guard("flow-ship", "git -C /tmp --no-pager -c user.name=test push")
    h.test_result(
        "git -C /tmp --no-pager -c ... push is detected and denied",
        code == 2,
        f"exit={code}"
    )

    # Test git with options that take separate arguments
    code, err = run_guard("flow-ship", "git --work-tree=/tmp --git-dir=/tmp/.git push")
    h.test_result(
        "git --work-tree=/tmp --git-dir=/tmp/.git push is denied",
        code == 2,
        f"exit={code}"
    )

    # Test gh with -R and other flags
    code, err = run_guard("flow-implement", "gh -R myrepo --json name pr create")
    h.test_result(
        "gh -R repo --json pr create is detected and denied",
        code == 2,
        f"exit={code}"
    )

    print()
    print("[Test Group 6] Positive control: known safe patterns still work\n")

    # Verify that legitimate git commands still work after subcommand finder changes
    code, err = run_guard("flow-implement", "git status")
    h.test_result(
        "git status still allowed (control)",
        code == 0,
        f"exit={code}"
    )

    code, err = run_guard("flow-implement", "git add -A")
    h.test_result(
        "git add -A still allowed (control)",
        code == 0,
        f"exit={code}"
    )

    # Legitimate gh commands
    code, err = run_guard("flow-implement", "gh issue list")
    h.test_result(
        "gh issue list still allowed (control)",
        code == 0,
        f"exit={code}"
    )

    print()
    print("[Test Group 7] Edge case: command ends at global options (no subcommand)\n")

    # Test that commands ending after global options don't crash and are denied
    code, err = run_guard("flow-ship", "git -C /tmp")
    h.test_result(
        "git -C /tmp (no subcommand) is denied or allowed consistently",
        code in (0, 2),
        f"exit={code} (should not crash)"
    )

    code, err = run_guard("flow-implement", "gh -R myrepo")
    h.test_result(
        "gh -R repo (no subcommand) is denied or allowed consistently",
        code in (0, 2),
        f"exit={code} (should not crash)"
    )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
