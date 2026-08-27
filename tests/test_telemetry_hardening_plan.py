#!/usr/bin/env python3
"""
Test suite for telemetry-hardening plan items (second pass).

Covers:
1. append_event's ValueError contract: guards in install.sh (2>/dev/null || true)
2. telemetry_schema.py fchmod fix: file descriptor vs path chmod
3. check_stage_pairing.py caveat output to stderr (unconditional)
4. install.sh already-registered hooks detection (no-flag branch)

Run with: python3 tests/test_telemetry_hardening_plan.py
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

# Add parent/scripts to path so we can import telemetry_schema
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import telemetry_schema

from _test_harness import REPO_ROOT, Harness

SCRIPTS_DIR = REPO_ROOT / "scripts"
CHECK_STAGE_PAIRING = SCRIPTS_DIR / "check_stage_pairing.py"
INSTALL_SH = REPO_ROOT / "install.sh"


def run_script(args, cwd=None, stdin_text=None):
    """Run a script as a subprocess. Returns (returncode, stdout, stderr)."""
    cmd = [sys.executable] + args
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        input=stdin_text,
    )
    return result.returncode, result.stdout, result.stderr


def run_bash_script(args, cwd=None, stdin_text=None):
    """Run a bash script as a subprocess. Returns (returncode, stdout, stderr)."""
    cmd = ["bash"] + args
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd,
        input=stdin_text,
    )
    return result.returncode, result.stdout, result.stderr


# ============================================================================
# Section 1: append_event ValueError contract & install.sh guards
# ============================================================================

def test_append_event_raises_valueerror_for_invalid_event():
    """append_event raises ValueError when validation fails."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"

        # Build an invalid event (missing session_id)
        invalid_event = {
            "schema_version": telemetry_schema.SCHEMA_VERSION,
            "event_type": "session.begin",
            "timestamp": "2026-08-26T12:00:00Z",
            # Missing session_id!
        }

        try:
            telemetry_schema.append_event(log_path, invalid_event)
            return False, "should have raised ValueError"
        except ValueError as e:
            return True, ""


def test_append_event_raises_valueerror_for_bad_timestamp():
    """append_event raises ValueError for malformed timestamp."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"

        # Build an invalid event (bad timestamp)
        invalid_event = {
            "schema_version": telemetry_schema.SCHEMA_VERSION,
            "event_type": "session.begin",
            "session_id": "123",
            "timestamp": "this-is-not-a-timestamp",
        }

        try:
            telemetry_schema.append_event(log_path, invalid_event)
            return False, "should have raised ValueError for bad timestamp"
        except ValueError as e:
            return True, ""


# ============================================================================
# Section 2: telemetry_schema.py fchmod fix
# ============================================================================

def test_append_event_uses_fchmod_not_chmod():
    """append_event successfully creates files using fchmod (not chmod on fd)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_path = Path(tmpdir) / "test.jsonl"

        # Build a valid event
        event = telemetry_schema.build_event(
            "session.begin",
            session_id="test-session",
            timestamp="2026-08-26T12:00:00Z",
        )

        try:
            # This should succeed - if the implementation was using os.chmod(fd, mode)
            # instead of os.fchmod(fd, mode), it would raise TypeError because fd is an int
            telemetry_schema.append_event(log_path, event)

            # Verify file was created
            if not log_path.exists():
                return False, "file not created"

            # Verify the file has correct permissions (mode 0600)
            file_mode = stat.S_IMODE(log_path.stat().st_mode)
            if file_mode != 0o600:
                return False, f"expected mode 0o600, got 0o{file_mode:03o}"

            return True, ""

        except TypeError as e:
            if "integer" in str(e).lower() or "path" in str(e).lower():
                return False, f"bug: using os.chmod(fd, mode) instead of os.fchmod(fd, mode): {e}"
            raise


def test_append_event_parent_dir_mode_0700():
    """append_event creates parent directory at mode 0700."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a path where the parent dir doesn't exist yet
        parent_path = Path(tmpdir) / "telemetry_dir"
        log_path = parent_path / "events.jsonl"

        event = telemetry_schema.build_event(
            "session.begin",
            session_id="test-session",
            timestamp="2026-08-26T12:00:00Z",
        )

        telemetry_schema.append_event(log_path, event)

        if not parent_path.exists():
            return False, "parent directory not created"

        if not parent_path.is_dir():
            return False, "parent path is not a directory"

        dir_mode = stat.S_IMODE(parent_path.stat().st_mode)
        if dir_mode != 0o700:
            return False, f"expected parent mode 0o700, got 0o{dir_mode:03o}"

        return True, ""


# ============================================================================
# Section 3: check_stage_pairing.py caveat output
# ============================================================================

def test_check_stage_pairing_prints_caveat_to_stderr_with_findings():
    """check_stage_pairing.py prints caveat to stderr when findings exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "test.md"

        # Write markdown with an orphaned begin (has findings)
        content = """```bash
run-metrics.py stage-begin --stage build --log "$LOG"
# Missing stage-end!
```
"""
        test_file.write_text(content)

        code, stdout, stderr = run_script([str(CHECK_STAGE_PAIRING), str(test_file)])

        # Should exit non-zero
        if code == 0:
            return False, "should exit non-zero with findings"

        # Should print caveat to stderr
        if not stderr:
            return False, "caveat not printed to stderr (stderr is empty)"

        # Caveat should mention blind spots
        caveat_keywords = ["blind", "spot", "mismatch", "VAR", "scope", "leak"]
        has_caveat = any(kw.lower() in stderr.lower() for kw in caveat_keywords)
        if not has_caveat:
            return False, f"caveat in stderr missing expected keywords. stderr: {stderr}"

        return True, ""


def test_check_stage_pairing_prints_caveat_to_stderr_without_findings():
    """check_stage_pairing.py prints caveat to stderr unconditionally."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "test.md"

        # Write well-formed markdown (no findings)
        content = """```bash
run-metrics.py stage-begin --stage build --log "$LOG"
run-metrics.py stage-end --stage build --log "$LOG"
```
"""
        test_file.write_text(content)

        code, stdout, stderr = run_script([str(CHECK_STAGE_PAIRING), str(test_file)])

        # Should exit 0 (no findings)
        if code != 0:
            return False, f"should exit 0 without findings, got {code}. stdout: {stdout}"

        # Caveat should STILL be printed to stderr (unconditional)
        if not stderr:
            return False, "caveat not printed to stderr (stderr is empty) - caveat should be unconditional"

        # Caveat should mention blind spots
        caveat_keywords = ["blind", "spot", "mismatch", "VAR", "scope", "leak"]
        has_caveat = any(kw.lower() in stderr.lower() for kw in caveat_keywords)
        if not has_caveat:
            return False, f"caveat in stderr missing expected keywords. stderr: {stderr}"

        return True, ""


def test_check_stage_pairing_caveat_appears_after_findings():
    """Caveat to stderr appears after findings output (if any)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_file = Path(tmpdir) / "test.md"

        # Write markdown with an orphaned begin
        content = """```bash
run-metrics.py stage-begin --stage build --log "$LOG"
```
"""
        test_file.write_text(content)

        code, stdout, stderr = run_script([str(CHECK_STAGE_PAIRING), str(test_file)])

        # Should have both stdout findings and stderr caveat
        has_findings = "ORPHANED_BEGIN" in stdout
        if not has_findings:
            return False, f"no findings in stdout: {stdout}"

        has_caveat = len(stderr) > 0
        if not has_caveat:
            return False, "caveat not printed to stderr"

        return True, ""


# ============================================================================
# Section 4: install.sh already-registered hooks detection (no-flag branch)
# ============================================================================

def test_install_sh_exists():
    """install.sh script exists."""
    if not INSTALL_SH.exists():
        return False, f"install.sh not found at {INSTALL_SH}"
    return True, ""


def test_install_sh_has_telemetry_flag_logic():
    """install.sh contains logic for --with-telemetry flag."""
    try:
        with open(INSTALL_SH, 'r') as f:
            content = f.read()

        # Should have the flag check
        if "--with-telemetry" not in content:
            return False, "install.sh doesn't mention --with-telemetry flag"

        return True, ""
    except Exception as e:
        return False, f"error reading install.sh: {e}"


def test_install_sh_no_flag_checks_settings_json():
    """install.sh no-flag branch checks settings.json for already-registered hooks."""
    try:
        with open(INSTALL_SH, 'r') as f:
            content = f.read()

        # Should check settings.json when flag not passed
        if "settings.json" not in content or "jq" not in content:
            return False, "install.sh doesn't check settings.json via jq"

        # Should look for run-metrics in hook commands
        if "run-metrics" not in content:
            return False, "install.sh doesn't reference run-metrics in hook checking"

        return True, ""
    except Exception as e:
        return False, f"error reading install.sh: {e}"


def test_install_sh_no_flag_prints_already_registered_message():
    """install.sh no-flag branch prints already-registered message when hooks exist."""
    try:
        with open(INSTALL_SH, 'r') as f:
            content = f.read()

        # Should have message for already-registered hooks
        if "already registered" not in content.lower():
            return False, "install.sh missing 'already registered' message"

        # Should mention settings.json in the message
        if "settings.json" not in content:
            return False, "install.sh message doesn't mention settings.json"

        return True, ""
    except Exception as e:
        return False, f"error reading install.sh: {e}"


def test_install_sh_hooks_guarded_with_error_suppression():
    """install.sh hook registrations are guarded with 2>/dev/null || true."""
    try:
        with open(INSTALL_SH, 'r') as f:
            content = f.read()

        # Should have guards around hook commands
        # The guards should be `2>/dev/null || true` to suppress ValueError from append_event
        if "2>/dev/null || true" not in content:
            return False, "install.sh missing '2>/dev/null || true' guards"

        # Should have guards for each of the 4 hook types
        hook_types = ["SessionStart", "SessionEnd", "SubagentStart", "SubagentStop"]
        found_hooks = 0
        for hook_type in hook_types:
            if hook_type in content:
                found_hooks += 1

        if found_hooks < 4:
            return False, f"install.sh only mentions {found_hooks}/4 hook types"

        return True, ""
    except Exception as e:
        return False, f"error reading install.sh: {e}"


if __name__ == "__main__":
    h = Harness("TELEMETRY HARDENING PLAN TEST SUITE")

    test_result = h.test_result

    # ========================================================================
    # Section 1: append_event ValueError contract
    # ========================================================================
    print("[Section 1] append_event ValueError contract")
    passed, msg = test_append_event_raises_valueerror_for_invalid_event()
    test_result("append_event raises ValueError for invalid event", passed, msg)

    passed, msg = test_append_event_raises_valueerror_for_bad_timestamp()
    test_result("append_event raises ValueError for bad timestamp", passed, msg)

    print()

    # ========================================================================
    # Section 2: telemetry_schema.py fchmod fix
    # ========================================================================
    print("[Section 2] telemetry_schema.py fchmod fix")
    passed, msg = test_append_event_uses_fchmod_not_chmod()
    test_result("append_event uses fchmod (not chmod on fd)", passed, msg)

    passed, msg = test_append_event_parent_dir_mode_0700()
    test_result("append_event creates parent dir at mode 0700", passed, msg)

    print()

    # ========================================================================
    # Section 3: check_stage_pairing.py caveat output
    # ========================================================================
    print("[Section 3] check_stage_pairing.py caveat output")
    passed, msg = test_check_stage_pairing_prints_caveat_to_stderr_with_findings()
    test_result("caveat printed to stderr when findings exist", passed, msg)

    passed, msg = test_check_stage_pairing_prints_caveat_to_stderr_without_findings()
    test_result("caveat printed to stderr unconditionally", passed, msg)

    passed, msg = test_check_stage_pairing_caveat_appears_after_findings()
    test_result("caveat appears after findings output", passed, msg)

    print()

    # ========================================================================
    # Section 4: install.sh already-registered hooks detection
    # ========================================================================
    print("[Section 4] install.sh no-flag branch and hook guards")
    passed, msg = test_install_sh_exists()
    test_result("install.sh exists", passed, msg)

    passed, msg = test_install_sh_has_telemetry_flag_logic()
    test_result("install.sh has --with-telemetry flag logic", passed, msg)

    passed, msg = test_install_sh_hooks_guarded_with_error_suppression()
    test_result("hook registrations guarded with 2>/dev/null || true", passed, msg)

    passed, msg = test_install_sh_no_flag_checks_settings_json()
    test_result("no-flag branch checks settings.json for hooks", passed, msg)

    passed, msg = test_install_sh_no_flag_prints_already_registered_message()
    test_result("no-flag branch prints already-registered message", passed, msg)

    print()

    h.summarize_and_exit()
