#!/usr/bin/env python3
"""
Test suite for flow agent hook registration in install.sh.

Covers:
1. Registration adds the 3 hooks to a fresh settings.json
2. Idempotent on a second run (no duplicates)
3. Preserves pre-existing unrelated hooks/keys
4. Skips invalid-JSON settings with a warning and leaves the file unchanged
5. --check reports drift when hooks are absent and passes when present
6. Agent files reference scripts that exist

Run with: python3 tests/test_flow_hook_registration.py
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from _test_harness import REPO_ROOT, Harness

INSTALL_SH = REPO_ROOT / "install.sh"


def run_bash_script(args, cwd=None, env=None):
    """Run a bash script as a subprocess. Returns (returncode, stdout, stderr)."""
    cmd = ["bash"] + args
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


def setup_test_repo_with_install_sh(tmpdir_path, source_repo):
    """Copy install.sh into the source_repo so REPO_DIR resolves correctly."""
    install_sh_copy = source_repo / "install.sh"
    shutil.copy(INSTALL_SH, install_sh_copy)
    install_sh_copy.chmod(0o755)
    return install_sh_copy


def test_registration_adds_hooks_to_fresh_settings():
    """Registration adds the 3 hooks to a fresh settings.json."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        test_home = tmpdir_path / "home"
        test_home.mkdir()

        source_repo = tmpdir_path / "repo"
        source_repo.mkdir()
        # Create minimal repo structure
        for dirname in ["commands", "reviewers", "prompts", "agents", "scripts"]:
            (source_repo / dirname).mkdir()
            (source_repo / dirname / "keep.md").write_text("placeholder\n")
        (source_repo / "prompts" / "preferences.yaml.template").write_text("# template\n")

        install_sh_copy = setup_test_repo_with_install_sh(tmpdir_path, source_repo)

        env = os.environ.copy()
        env["HOME"] = str(test_home)

        # Run install.sh
        code, stdout, stderr = run_bash_script([str(install_sh_copy)], env=env)

        if code != 0:
            return False, f"install.sh failed with code {code}: {stderr}"

        # Check that settings.json was created with hooks
        settings_file = test_home / ".claude" / "settings.json"
        if not settings_file.exists():
            return False, "settings.json was not created"

        try:
            settings = json.loads(settings_file.read_text())
        except json.JSONDecodeError as e:
            return False, f"settings.json is not valid JSON: {e}"

        # Check for PreToolUse hooks
        pre_tool_use = settings.get("hooks", {}).get("PreToolUse", [])
        if not pre_tool_use:
            return False, "No PreToolUse hooks registered"

        # Check that all 3 matchers are present
        matchers = {entry["matcher"] for entry in pre_tool_use}
        expected_matchers = {"Agent|Task", "Skill", "Bash"}
        if matchers != expected_matchers:
            return False, f"Expected matchers {expected_matchers}, got {matchers}"

        return True, ""


def test_registration_is_idempotent():
    """Registration is idempotent on a second run (no duplicates)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        test_home = tmpdir_path / "home"
        test_home.mkdir()

        source_repo = tmpdir_path / "repo"
        source_repo.mkdir()
        for dirname in ["commands", "reviewers", "prompts", "agents", "scripts"]:
            (source_repo / dirname).mkdir()
            (source_repo / dirname / "keep.md").write_text("placeholder\n")
        (source_repo / "prompts" / "preferences.yaml.template").write_text("# template\n")

        install_sh_copy = setup_test_repo_with_install_sh(tmpdir_path, source_repo)

        env = os.environ.copy()
        env["HOME"] = str(test_home)

        # First run
        code, _, _ = run_bash_script([str(install_sh_copy)], env=env)
        if code != 0:
            return False, "first run failed"

        # Read settings.json after first run
        settings_file = test_home / ".claude" / "settings.json"
        settings_first = json.loads(settings_file.read_text())
        first_count = len(settings_first.get("hooks", {}).get("PreToolUse", []))

        # Second run (idempotent)
        code, _, _ = run_bash_script([str(install_sh_copy)], env=env)
        if code != 0:
            return False, "second run failed"

        # Read settings.json after second run
        settings_second = json.loads(settings_file.read_text())
        second_count = len(settings_second.get("hooks", {}).get("PreToolUse", []))

        if first_count != second_count:
            return False, f"idempotency failed: first run had {first_count} hooks, second had {second_count}"

        return True, ""


def test_registration_preserves_unrelated_hooks():
    """Registration preserves pre-existing unrelated hooks/keys."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        test_home = tmpdir_path / "home"
        test_home.mkdir()

        source_repo = tmpdir_path / "repo"
        source_repo.mkdir()
        for dirname in ["commands", "reviewers", "prompts", "agents", "scripts"]:
            (source_repo / dirname).mkdir()
            (source_repo / dirname / "keep.md").write_text("placeholder\n")
        (source_repo / "prompts" / "preferences.yaml.template").write_text("# template\n")

        # Pre-create settings.json with an unrelated hook
        settings_file = test_home / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        pre_existing_settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python3 ~/.claude/scripts/run-metrics.py session-begin 2>/dev/null || true",
                            }
                        ]
                    }
                ],
                "otherKey": "otherValue",
            }
        }
        settings_file.write_text(json.dumps(pre_existing_settings))

        install_sh_copy = setup_test_repo_with_install_sh(tmpdir_path, source_repo)

        env = os.environ.copy()
        env["HOME"] = str(test_home)

        # Run install.sh
        code, _, _ = run_bash_script([str(install_sh_copy)], env=env)
        if code != 0:
            return False, "install.sh failed"

        # Check that pre-existing hooks and keys are preserved
        settings = json.loads(settings_file.read_text())
        if "SessionStart" not in settings.get("hooks", {}):
            return False, "SessionStart hook was removed"
        if settings.get("hooks", {}).get("otherKey") != "otherValue":
            return False, "otherKey was removed or modified"

        # Check that PreToolUse hooks were added
        pre_tool_use = settings.get("hooks", {}).get("PreToolUse", [])
        if not pre_tool_use:
            return False, "PreToolUse hooks were not added"

        return True, ""


def test_registration_skips_invalid_json_with_warning():
    """Registration skips invalid-JSON settings with a warning and leaves the file unchanged."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        test_home = tmpdir_path / "home"
        test_home.mkdir()

        source_repo = tmpdir_path / "repo"
        source_repo.mkdir()
        for dirname in ["commands", "reviewers", "prompts", "agents", "scripts"]:
            (source_repo / dirname).mkdir()
            (source_repo / dirname / "keep.md").write_text("placeholder\n")
        (source_repo / "prompts" / "preferences.yaml.template").write_text("# template\n")

        # Pre-create settings.json with invalid JSON
        settings_file = test_home / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        invalid_json = '{ "invalid": json }'
        settings_file.write_text(invalid_json)

        install_sh_copy = setup_test_repo_with_install_sh(tmpdir_path, source_repo)

        env = os.environ.copy()
        env["HOME"] = str(test_home)

        # Run install.sh
        code, stdout, stderr = run_bash_script([str(install_sh_copy)], env=env)

        # Should succeed (returns 0) but warn about invalid JSON
        if code != 0:
            return False, f"install.sh should not fail on invalid JSON, got code {code}"

        # Check that warning was issued
        output = stdout + stderr
        if "warning" not in output.lower() or "json" not in output.lower():
            return False, f"expected warning about JSON, got: {output}"

        # Check that file was NOT modified
        if settings_file.read_text() != invalid_json:
            return False, "invalid JSON file should not have been modified"

        return True, ""


def test_check_reports_drift_without_hooks():
    """--check reports drift when hooks are absent."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        test_home = tmpdir_path / "home"
        test_home.mkdir()

        source_repo = tmpdir_path / "repo"
        source_repo.mkdir()
        for dirname in ["commands", "reviewers", "prompts", "agents", "scripts"]:
            (source_repo / dirname).mkdir()
            (source_repo / dirname / "keep.md").write_text("placeholder\n")
        (source_repo / "prompts" / "preferences.yaml.template").write_text("# template\n")

        # Create settings.json WITHOUT the hooks
        settings_file = test_home / ".claude" / "settings.json"
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps({"hooks": {}}))

        install_sh_copy = setup_test_repo_with_install_sh(tmpdir_path, source_repo)

        env = os.environ.copy()
        env["HOME"] = str(test_home)

        # Run install.sh --check
        code, stdout, stderr = run_bash_script([str(install_sh_copy), "--check"], env=env)

        # Should exit 1 (drift detected)
        if code == 0:
            return False, "--check should exit 1 when hooks are missing"

        # Should report hook-missing
        output = stdout + stderr
        if "hook-missing" not in output:
            return False, f"expected 'hook-missing' in output, got: {output}"

        return True, ""


def test_check_passes_with_hooks():
    """--check passes when hooks are present."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        test_home = tmpdir_path / "home"
        test_home.mkdir()

        source_repo = tmpdir_path / "repo"
        source_repo.mkdir()
        for dirname in ["commands", "reviewers", "prompts", "agents", "scripts"]:
            (source_repo / dirname).mkdir()
            (source_repo / dirname / "keep.md").write_text("placeholder\n")
        (source_repo / "prompts" / "preferences.yaml.template").write_text("# template\n")

        install_sh_copy = setup_test_repo_with_install_sh(tmpdir_path, source_repo)

        env = os.environ.copy()
        env["HOME"] = str(test_home)

        # First, run install.sh to register hooks
        code, _, _ = run_bash_script([str(install_sh_copy)], env=env)
        if code != 0:
            return False, "initial install.sh failed"

        # Then run install.sh --check
        code, stdout, stderr = run_bash_script([str(install_sh_copy), "--check"], env=env)

        # Should exit 0 (no drift)
        if code != 0:
            return False, f"--check should exit 0 after hooks are registered, got code {code}"

        # Should report no drift or "No drift"
        output = stdout + stderr
        if "drift" not in output.lower() and "No drift" not in output:
            # It's okay if there's no explicit message, as long as exit code is 0
            pass

        return True, ""


def test_agent_files_reference_existing_scripts():
    """Agent files (flow-implement.md, flow-review.md) reference scripts that exist."""
    # Read agents/flow-implement.md
    flow_implement_path = REPO_ROOT / "agents" / "flow-implement.md"
    if not flow_implement_path.exists():
        return False, f"agents/flow-implement.md not found at {flow_implement_path}"

    flow_implement_content = flow_implement_path.read_text()

    # Extract script paths from hook commands in frontmatter (pattern: scripts/flow-*.py)
    script_pattern = r'scripts/(flow-[a-z-]+\.py)'
    matches = set(re.findall(script_pattern, flow_implement_content))

    if not matches:
        return False, "no script references found in flow-implement.md"

    for script_name in matches:
        script_path = REPO_ROOT / "scripts" / script_name
        if not script_path.exists():
            return False, f"referenced script not found: {script_path}"

    # Do the same for flow-review.md
    flow_review_path = REPO_ROOT / "agents" / "flow-review.md"
    if not flow_review_path.exists():
        return False, f"agents/flow-review.md not found at {flow_review_path}"

    flow_review_content = flow_review_path.read_text()
    matches_review = set(re.findall(script_pattern, flow_review_content))

    if not matches_review:
        return False, "no script references found in flow-review.md"

    for script_name in matches_review:
        script_path = REPO_ROOT / "scripts" / script_name
        if not script_path.exists():
            return False, f"referenced script not found: {script_path}"

    return True, ""


if __name__ == "__main__":
    h = Harness("FLOW AGENT HOOK REGISTRATION TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Hook registration behavior")
    passed, msg = test_registration_adds_hooks_to_fresh_settings()
    test_result("registration adds 3 hooks to fresh settings.json", passed, msg)

    passed, msg = test_registration_is_idempotent()
    test_result("registration is idempotent (no duplicates on second run)", passed, msg)

    passed, msg = test_registration_preserves_unrelated_hooks()
    test_result("registration preserves pre-existing unrelated hooks/keys", passed, msg)

    print()

    print("[Section 2] Error handling")
    passed, msg = test_registration_skips_invalid_json_with_warning()
    test_result("registration skips invalid JSON with warning", passed, msg)

    print()

    print("[Section 3] --check behavior")
    passed, msg = test_check_reports_drift_without_hooks()
    test_result("--check reports drift when hooks are absent", passed, msg)

    passed, msg = test_check_passes_with_hooks()
    test_result("--check passes when hooks are present", passed, msg)

    print()

    print("[Section 4] Agent file validation")
    passed, msg = test_agent_files_reference_existing_scripts()
    test_result("agent files reference scripts that exist", passed, msg)

    print()

    h.summarize_and_exit()
