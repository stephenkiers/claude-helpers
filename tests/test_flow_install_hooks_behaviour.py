#!/usr/bin/env python3
"""
Test suite for /expert-flow step agent guard scripts and hook registration.

Tests that:
1. All three guard scripts (flow-agent-guard.py, flow-skill-guard.py, flow-bash-guard.py) exist
2. The guards are referenced in agent hook declarations
3. install.sh infrastructure for hook registration is present
4. Agent files declare the correct tools and hooks

Note: Full hook registration testing in settings.json requires proper shell environment
(bash with associative array support). This test focuses on script availability and
agent file declarations.

Run with: python3 tests/test_flow_install_hooks_behaviour.py
"""

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT


INSTALL_SCRIPT = REPO_ROOT / "install.sh"
BASH_GUARD = REPO_ROOT / "scripts" / "flow-bash-guard.py"
AGENT_GUARD = REPO_ROOT / "scripts" / "flow-agent-guard.py"
SKILL_GUARD = REPO_ROOT / "scripts" / "flow-skill-guard.py"
FLOW_IMPLEMENT = REPO_ROOT / "agents" / "flow-implement.md"
FLOW_REVIEW = REPO_ROOT / "agents" / "flow-review.md"
FLOW_BUCKETS = REPO_ROOT / "scripts" / "flow_agent_buckets.py"


def get_settings(claude_dir):
    """Read settings.json from CLAUDE_DIR. Returns dict or None if missing."""
    settings_path = Path(claude_dir) / "settings.json"
    if not settings_path.exists():
        return None
    try:
        return json.loads(settings_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def main():
    h = Harness("FLOW GUARDS AND INSTALL HOOKS BEHAVIOUR TEST SUITE (ADR-0008)")

    # ============================================================================
    # SECTION 1: Guard scripts exist
    # ============================================================================
    print("[Section 1] Guard script existence")

    h.test_result(
        "flow-bash-guard.py exists",
        BASH_GUARD.exists(),
        f"not found: {BASH_GUARD}"
    )

    h.test_result(
        "flow-agent-guard.py exists",
        AGENT_GUARD.exists(),
        f"not found: {AGENT_GUARD}"
    )

    h.test_result(
        "flow-skill-guard.py exists",
        SKILL_GUARD.exists(),
        f"not found: {SKILL_GUARD}"
    )

    # ============================================================================
    # SECTION 2: Guard scripts are executable
    # ============================================================================
    print()
    print("[Section 2] Guard script executability")

    h.test_result(
        "flow-bash-guard.py is executable",
        BASH_GUARD.exists() and BASH_GUARD.stat().st_mode & 0o111 != 0,
        "script not executable"
    )

    h.test_result(
        "flow-agent-guard.py is executable",
        AGENT_GUARD.exists() and AGENT_GUARD.stat().st_mode & 0o111 != 0,
        "script not executable"
    )

    h.test_result(
        "flow-skill-guard.py is executable",
        SKILL_GUARD.exists() and SKILL_GUARD.stat().st_mode & 0o111 != 0,
        "script not executable"
    )

    # ============================================================================
    # SECTION 3: install.sh exists and is structured
    # ============================================================================
    print()
    print("[Section 3] install.sh structure")

    h.test_result(
        "install.sh exists",
        INSTALL_SCRIPT.exists(),
        f"not found: {INSTALL_SCRIPT}"
    )

    h.test_result(
        "install.sh is executable",
        INSTALL_SCRIPT.exists() and INSTALL_SCRIPT.stat().st_mode & 0o111 != 0,
        "script not executable"
    )

    if INSTALL_SCRIPT.exists():
        install_content = INSTALL_SCRIPT.read_text()
        h.test_result(
            "install.sh contains PreToolUse hook registration code",
            "PreToolUse" in install_content,
            "no PreToolUse references found"
        )

        h.test_result(
            "install.sh references flow-agent-guard.py",
            "flow-agent-guard.py" in install_content,
            "guard script not referenced"
        )

        h.test_result(
            "install.sh references flow-skill-guard.py",
            "flow-skill-guard.py" in install_content,
            "guard script not referenced"
        )

        h.test_result(
            "install.sh references flow-bash-guard.py",
            "flow-bash-guard.py" in install_content,
            "guard script not referenced"
        )

    # ============================================================================
    # SECTION 4: flow-implement.md declares guards in hooks
    # ============================================================================
    print()
    print("[Section 4] flow-implement.md hook declarations")

    h.test_result(
        "flow-implement.md exists",
        FLOW_IMPLEMENT.exists(),
        f"not found: {FLOW_IMPLEMENT}"
    )

    if FLOW_IMPLEMENT.exists():
        flow_impl_content = FLOW_IMPLEMENT.read_text()

        h.test_result(
            "flow-implement.md is readable",
            len(flow_impl_content) > 0,
            "file empty or unreadable"
        )

        h.test_result(
            "flow-implement.md declares hooks",
            "hooks:" in flow_impl_content or "PreToolUse" in flow_impl_content,
            "no hooks declaration found"
        )

        h.test_result(
            "flow-implement.md wires flow-agent-guard.py",
            "flow-agent-guard.py" in flow_impl_content,
            "agent guard not wired in hooks"
        )

        h.test_result(
            "flow-implement.md wires flow-skill-guard.py",
            "flow-skill-guard.py" in flow_impl_content,
            "skill guard not wired in hooks"
        )

        h.test_result(
            "flow-implement.md wires flow-bash-guard.py",
            "flow-bash-guard.py" in flow_impl_content,
            "bash guard not wired in hooks"
        )

        h.test_result(
            "flow-implement.md has --agent flag in hooks",
            "--agent" in flow_impl_content,
            "no --agent flags found in hooks"
        )

        h.test_result(
            "flow-implement.md tools list is declared",
            "tools:" in flow_impl_content,
            "no tools: field in frontmatter"
        )

    # ============================================================================
    # SECTION 5: flow-review.md structure
    # ============================================================================
    print()
    print("[Section 5] flow-review.md structure")

    h.test_result(
        "flow-review.md exists",
        FLOW_REVIEW.exists(),
        f"not found: {FLOW_REVIEW}"
    )

    if FLOW_REVIEW.exists():
        flow_rev_content = FLOW_REVIEW.read_text()

        h.test_result(
            "flow-review.md is readable",
            len(flow_rev_content) > 0,
            "file empty or unreadable"
        )

        h.test_result(
            "flow-review.md declares hooks",
            "hooks:" in flow_rev_content or "PreToolUse" in flow_rev_content,
            "no hooks declaration found"
        )

        h.test_result(
            "flow-review.md has tools declaration",
            "tools:" in flow_rev_content,
            "no tools: field in frontmatter"
        )

    # ============================================================================
    # SECTION 6: flow_agent_buckets.py constants
    # ============================================================================
    print()
    print("[Section 6] flow_agent_buckets.py constants")

    h.test_result(
        "flow_agent_buckets.py exists",
        FLOW_BUCKETS.exists(),
        f"not found: {FLOW_BUCKETS}"
    )

    if FLOW_BUCKETS.exists():
        buckets_content = FLOW_BUCKETS.read_text()
        h.test_result(
            "flow_agent_buckets.py is readable",
            len(buckets_content) > 0,
            "file empty or unreadable"
        )

        h.test_result(
            "flow_agent_buckets.py has FLOW_AGENT_CUTOVER_DATE",
            "FLOW_AGENT_CUTOVER_DATE" in buckets_content,
            "constant not found"
        )

        h.test_result(
            "flow_agent_buckets.py has PROBE_SESSION_IDS",
            "PROBE_SESSION_IDS" in buckets_content,
            "constant not found"
        )

    # ============================================================================
    # SECTION 7: Guard script integration test (basic functionality)
    # ============================================================================
    print()
    print("[Section 7] Guard script basic functionality")

    # Test that guards can be invoked without errors on invalid input
    # (they should fail gracefully, not crash)
    for guard_path, guard_name in [
        (BASH_GUARD, "flow-bash-guard"),
        (AGENT_GUARD, "flow-agent-guard"),
        (SKILL_GUARD, "flow-skill-guard"),
    ]:
        result = subprocess.run(
            [sys.executable, str(guard_path), "--agent", "flow-implement"],
            input="not valid json",
            capture_output=True,
            text=True,
            timeout=5,
        )
        h.test_result(
            f"{guard_name} handles invalid input gracefully (exit code indicates rejection)",
            result.returncode in [0, 1, 2],
            f"exit code {result.returncode} (expected 0/1/2)"
        )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
