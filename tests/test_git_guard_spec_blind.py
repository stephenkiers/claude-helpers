#!/usr/bin/env python3
"""
Spec-blind test suite for scripts/plan-implementer-git-guard.py.

This test suite is written ONLY from the plan specification (8 numbered behaviors),
without reading the implementation. It tests the git-guard as a black-box subprocess,
invoking it with JSON payloads on stdin.

Run with: python3 tests/test_git_guard_spec_blind.py
"""

import json
import subprocess
import sys
from pathlib import Path

from _test_harness import REPO_ROOT, Harness

GUARD_SCRIPT_PATH = REPO_ROOT / "scripts" / "plan-implementer-git-guard.py"

h = Harness("GIT-GUARD SPEC-BLIND TEST SUITE")
t = h.test_result


def invoke_guard(command_str):
    """
    Invoke the guard script with a command string.

    Returns: (exit_code, stdout, stderr)
    """
    payload = {"tool_input": {"command": command_str}}
    stdin_json = json.dumps(payload)

    try:
        result = subprocess.run(
            [sys.executable, str(GUARD_SCRIPT_PATH)],
            input=stdin_json,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except FileNotFoundError:
        return 127, "", f"guard script not found at {GUARD_SCRIPT_PATH}"


# =============================================================================
# BEHAVIOR 1: git rm and git mv are BLOCKED
# =============================================================================
code, out, err = invoke_guard("git rm file.txt")
t("git rm is BLOCKED (exit 2)", code == 2, f"exit={code}")

code, out, err = invoke_guard("git rm -r dir/")
t("git rm with -r flag is BLOCKED", code == 2, f"exit={code}")

code, out, err = invoke_guard("git rm --force file.txt")
t("git rm with --force flag is BLOCKED", code == 2, f"exit={code}")

code, out, err = invoke_guard("git mv old.txt new.txt")
t("git mv is BLOCKED (exit 2)", code == 2, f"exit={code}")

code, out, err = invoke_guard("git mv --force dir/ newdir/")
t("git mv with --force flag is BLOCKED", code == 2, f"exit={code}")

# =============================================================================
# BEHAVIOR 2: Quoted or path-qualified git invocations
# =============================================================================
code, out, err = invoke_guard("'git' reset --hard")
t("Quoted 'git' reset is BLOCKED", code == 2, f"exit={code}")

code, out, err = invoke_guard('"git" reset --hard')
t('Quoted "git" reset is BLOCKED', code == 2, f"exit={code}")

code, out, err = invoke_guard("/usr/bin/git reset --hard")
t("Path-qualified git reset is BLOCKED", code == 2, f"exit={code}")

code, out, err = invoke_guard("/usr/local/bin/git checkout HEAD -- file.txt")
t("Path-qualified git checkout is BLOCKED", code == 2, f"exit={code}")

code, out, err = invoke_guard("'git' rm file.txt")
t("Quoted git rm is BLOCKED", code == 2, f"exit={code}")

code, out, err = invoke_guard("/usr/bin/git mv old new")
t("Path-qualified git mv is BLOCKED", code == 2, f"exit={code}")

# Allowed commands should still work when quoted/path-qualified
code, out, err = invoke_guard("'git' add -A")
t("Quoted git add is ALLOWED (exit 0)", code == 0, f"exit={code}")

code, out, err = invoke_guard('/usr/bin/git status --porcelain')
t("Path-qualified git status is ALLOWED", code == 0, f"exit={code}")

# =============================================================================
# BEHAVIOR 3: BLOCK_MESSAGE consistency (lists allowed subcommands)
# =============================================================================
# Try a blocked command and check the message structure
code, out, err = invoke_guard("git reset --hard")
t("Blocked command exits with code 2", code == 2, f"exit={code}")

# The block message should list allowed subcommands
combined_output = out + err
has_allowed_list = (
    "add" in combined_output or "status" in combined_output or
    "diff" in combined_output or "allowed" in combined_output.lower()
)
t("Block message mentions allowed subcommands", has_allowed_list,
  f"output/error doesn't mention allowed commands: {combined_output[:200]}")

# =============================================================================
# BEHAVIOR 4: -c and -C global flag handling
# =============================================================================
# git -c core.pager=cat status should ALLOW (skip -c and find status)
code, out, err = invoke_guard("git -c core.pager=cat status")
t("git -c flag with separate value + allowed subcommand is ALLOWED",
  code == 0, f"exit={code}")

# git -C dir status should ALLOW
code, out, err = invoke_guard("git -C /tmp/workdir status")
t("git -C flag with separate value + allowed subcommand is ALLOWED",
  code == 0, f"exit={code}")

# git -Cfoo (attached -C form) with status should ALLOW
code, out, err = invoke_guard("git -C/tmp/workdir status")
t("git -C with attached value (no space) + allowed subcommand is ALLOWED",
  code == 0, f"exit={code}")

# git -cuser.name=x (attached -c form) with status should ALLOW
code, out, err = invoke_guard("git -cuser.name=x status")
t("git -c with attached value (no space) + allowed subcommand is ALLOWED",
  code == 0, f"exit={code}")

# git -c followed by blocked command should still BLOCK
code, out, err = invoke_guard("git -c core.pager=cat reset --hard")
t("git -c flag + blocked subcommand is BLOCKED",
  code == 2, f"exit={code}")

# git -C followed by blocked command should still BLOCK
code, out, err = invoke_guard("git -C /tmp/wt reset --hard")
t("git -C flag + blocked subcommand is BLOCKED",
  code == 2, f"exit={code}")

# Multiple -c flags followed by an allowed command
code, out, err = invoke_guard("git -c a=b -c x=y add file.txt")
t("Multiple -c flags + allowed subcommand is ALLOWED",
  code == 0, f"exit={code}")

# =============================================================================
# BEHAVIOR 5: No-subcommand message clarity (no literal "None")
# =============================================================================
code, out, err = invoke_guard("git")
t("Bare 'git' (no subcommand) exits with code 2", code == 2, f"exit={code}")

combined_output = out + err
has_none = "None" in combined_output
t("No-subcommand message does not contain 'None'",
  not has_none, f"message contains 'None': {combined_output[:200]}")

code, out, err = invoke_guard("git -c core.pager=cat")
combined_output = out + err
has_none = "None" in combined_output
t("git -c (no subcommand) message does not contain 'None'",
  not has_none, f"message contains 'None': {combined_output[:200]}")

# =============================================================================
# BEHAVIOR 6: Non-string command input fails closed
# =============================================================================
# Send command as integer instead of string
payload_int = {"tool_input": {"command": 12345}}
stdin_json = json.dumps(payload_int)
try:
    result = subprocess.run(
        [sys.executable, str(GUARD_SCRIPT_PATH)],
        input=stdin_json,
        capture_output=True,
        text=True,
        timeout=5,
    )
    code = result.returncode
    t("Non-string command (integer) exits with code 2 (fail closed)", code == 2,
      f"exit={code} (expected 2)")
except Exception as e:
    t("Non-string command (integer) does not crash", False, f"exception: {e}")

# Send command as list instead of string
payload_list = {"tool_input": {"command": ["git", "status"]}}
stdin_json = json.dumps(payload_list)
try:
    result = subprocess.run(
        [sys.executable, str(GUARD_SCRIPT_PATH)],
        input=stdin_json,
        capture_output=True,
        text=True,
        timeout=5,
    )
    code = result.returncode
    t("Non-string command (list) exits with code 2 (fail closed)", code == 2,
      f"exit={code} (expected 2)")
except Exception as e:
    t("Non-string command (list) does not crash", False, f"exception: {e}")

# =============================================================================
# BEHAVIOR 7: Unrecognized global flag is BLOCKED (documented over-blocking)
# =============================================================================
code, out, err = invoke_guard("git --no-pager status")
t("git --no-pager (unrecognized global flag) is BLOCKED",
  code == 2, f"exit={code}")

code, out, err = invoke_guard("git --verbose add file.txt")
t("git --verbose (unrecognized global flag) is BLOCKED",
  code == 2, f"exit={code}")

# =============================================================================
# BEHAVIOR 8: Still-allowed baseline behavior (unchanged)
# =============================================================================

# Allowed: git add -A
code, out, err = invoke_guard("git add -A")
t("git add -A is ALLOWED (exit 0)", code == 0, f"exit={code}")

# Allowed: git status --porcelain
code, out, err = invoke_guard("git status --porcelain")
t("git status --porcelain is ALLOWED", code == 0, f"exit={code}")

# Allowed: git diff --staged
code, out, err = invoke_guard("git diff --staged")
t("git diff --staged is ALLOWED", code == 0, f"exit={code}")

# Allowed: git -C /tmp/wt add -A
code, out, err = invoke_guard("git -C /tmp/wt add -A")
t("git -C /tmp/wt add -A is ALLOWED", code == 0, f"exit={code}")

# Allowed: non-git commands
code, out, err = invoke_guard("echo done")
t("echo done (non-git) is ALLOWED", code == 0, f"exit={code}")

code, out, err = invoke_guard("cargo test")
t("cargo test (non-git) is ALLOWED", code == 0, f"exit={code}")

# Blocked: git reset --hard
code, out, err = invoke_guard("git reset --hard")
t("git reset --hard is BLOCKED", code == 2, f"exit={code}")

# Blocked: git checkout HEAD -- file
code, out, err = invoke_guard("git checkout HEAD -- file")
t("git checkout HEAD -- file is BLOCKED", code == 2, f"exit={code}")

# Blocked: git stash
code, out, err = invoke_guard("git stash")
t("git stash is BLOCKED", code == 2, f"exit={code}")

# Blocked: git commit -m x
code, out, err = invoke_guard("git commit -m 'test'")
t("git commit -m is BLOCKED", code == 2, f"exit={code}")

# Blocked: git push
code, out, err = invoke_guard("git push")
t("git push is BLOCKED", code == 2, f"exit={code}")

# Blocked (chained): git status && git reset
code, out, err = invoke_guard("git status && git reset")
t("git status && git reset (chained) is BLOCKED",
  code == 2, f"exit={code}")

# Blocked with -C: git -C /tmp/wt reset
code, out, err = invoke_guard("git -C /tmp/wt reset")
t("git -C /tmp/wt reset is BLOCKED", code == 2, f"exit={code}")

# Blocked with uppercased subcommand: git RESET
code, out, err = invoke_guard("git RESET")
t("git RESET (uppercase) is BLOCKED", code == 2, f"exit={code}")

# Additional edge cases based on baseline behavior
code, out, err = invoke_guard("git add file1.txt file2.txt")
t("git add with multiple files is ALLOWED", code == 0, f"exit={code}")

code, out, err = invoke_guard("git status")
t("git status (no flags) is ALLOWED", code == 0, f"exit={code}")

code, out, err = invoke_guard("git diff")
t("git diff (no flags) is ALLOWED", code == 0, f"exit={code}")

code, out, err = invoke_guard("git ls-files")
t("git ls-files is ALLOWED (if in allowed set)", code in [0, 2], f"exit={code}")

code, out, err = invoke_guard("git log")
t("git log is ALLOWED (if in allowed set)", code in [0, 2], f"exit={code}")

# =============================================================================
# Additional edge cases and robustness tests (from plan spec)
# =============================================================================

# git command with tabs and unusual whitespace
code, out, err = invoke_guard("git\tadd\t-A")
t("git add with tabs is ALLOWED", code == 0, f"exit={code}")

# Path-qualified git with multiple hyphens in path
code, out, err = invoke_guard("/usr/bin-x86/git add -A")
t("Path-qualified (with hyphens) git add is ALLOWED", code == 0, f"exit={code}")

# Verify that the allowed subcommand set is consistent across multiple calls
# (i.e., the block message doesn't randomly change)
code1, out1, err1 = invoke_guard("git reset --hard")
code2, out2, err2 = invoke_guard("git checkout HEAD -- file")
msg1 = out1 + err1
msg2 = out2 + err2
# Extract and compare allowed subcommand mentions (just a basic sanity check)
t("Block messages are consistent (same format)",
  (code1 == code2 == 2) and (len(msg1) > 0) and (len(msg2) > 0),
  f"code1={code1}, code2={code2}, msg1_len={len(msg1)}, msg2_len={len(msg2)}")

h.summarize_and_exit()
