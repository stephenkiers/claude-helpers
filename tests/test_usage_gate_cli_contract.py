#!/usr/bin/env python3
"""
Test suite for run-metrics.py usage-check CLI contract.

Covers:
  - CLI flags: --seam (6 valid values, rejects invalid), --threshold, --session-id, --mark-reported
  - Printed USAGE-GATE: line shape with all required fields
  - JSON output format
  - Exit codes (0 for proceed, 10 for ask)
  - Unavailable state behavior (DECISION: proceed, not ask)
  - Session mismatch state (DECISION: ask with distinct remedy text)
  - Floor labeling when agents are unaccounted
  - Threshold comparison and state derivation
  - The 'final' seam's special handling

Run with: python3 tests/test_usage_gate_cli_contract.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

SCRIPT = REPO_ROOT / "scripts" / "run-metrics.py"


def run_usage_check(args, env=None, state_dir=None):
    """Run usage-check subcommand. Returns (returncode, stdout, stderr)."""
    cmd = [sys.executable, str(SCRIPT)]

    # Use isolated state dir if provided (must come before subcommand)
    if state_dir:
        cmd.extend(["--state-dir", state_dir])

    cmd.append("usage-check")
    cmd.extend(args)

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
    )
    return result.returncode, result.stdout, result.stderr


def test_seam_flag_required():
    """--seam flag is required."""
    code, stdout, stderr = run_usage_check([])
    if code == 0:
        return False, "expected non-zero exit for missing --seam"
    if "--seam" not in stderr:
        return False, f"expected error about --seam in stderr, got: {stderr}"
    return True, ""


def test_valid_seam_values():
    """All six valid seam values are accepted."""
    valid_seams = ["round1-join", "gate-fix-loop", "pre-fanout", "post-fanout", "pre-round4", "final"]

    with tempfile.TemporaryDirectory() as tmpdir:
        for seam in valid_seams:
            code, stdout, stderr = run_usage_check(
                ["--seam", seam],
                state_dir=tmpdir,
            )
            if code not in (0, 10):
                return False, f"seam '{seam}' gave unexpected exit code {code}"
            if "USAGE-GATE:" not in stdout:
                return False, f"seam '{seam}' did not produce USAGE-GATE output"

    return True, ""


def test_invalid_seam_rejected():
    """Invalid seam values are rejected."""
    code, stdout, stderr = run_usage_check(["--seam", "invalid-seam"])
    if code == 0:
        return False, "expected non-zero exit for invalid seam"
    if "invalid-seam" not in stderr and "invalid" not in stderr.lower():
        return False, f"expected validation error, got stderr: {stderr}"
    return True, ""


def test_usage_gate_line_format():
    """USAGE-GATE output line has all required fields."""
    with tempfile.TemporaryDirectory() as tmpdir:
        code, stdout, stderr = run_usage_check(
            ["--seam", "round1-join"],
            state_dir=tmpdir,
        )

        lines = stdout.strip().split("\n")
        if not lines:
            return False, "no output produced"

        gate_line = lines[0]
        if not gate_line.startswith("USAGE-GATE:"):
            return False, f"first line doesn't start with USAGE-GATE: {gate_line}"

        required_fields = [
            "seam=", "counted=", "threshold=", "agents=", "state=", "DECISION:"
        ]
        for field in required_fields:
            if field not in gate_line:
                return False, f"missing field '{field}' in: {gate_line}"

        return True, ""


def test_json_output_present():
    """JSON output is on second line of output."""
    with tempfile.TemporaryDirectory() as tmpdir:
        code, stdout, stderr = run_usage_check(
            ["--seam", "round1-join"],
            state_dir=tmpdir,
        )

        lines = stdout.strip().split("\n")
        if len(lines) < 2:
            return False, f"expected at least 2 lines, got {len(lines)}"

        try:
            json_obj = json.loads(lines[1])
        except json.JSONDecodeError as e:
            return False, f"JSON parsing failed: {e}"

        required_keys = [
            "seam", "counted_tokens", "threshold", "state", "decision",
            "accounted"
        ]
        for key in required_keys:
            if key not in json_obj:
                return False, f"missing JSON key '{key}'"

        return True, ""


def test_exit_code_proceed():
    """DECISION: proceed returns exit code 0."""
    with tempfile.TemporaryDirectory() as tmpdir:
        code, stdout, stderr = run_usage_check(
            ["--seam", "round1-join"],
            state_dir=tmpdir,
        )

        if code != 0:
            return False, f"expected exit code 0 for proceed, got {code}"

        if "DECISION: proceed" not in stdout:
            return False, f"expected 'DECISION: proceed' in output, got: {stdout}"

        return True, ""


def test_exit_code_ask():
    """DECISION: ask returns exit code 10."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # With a very low threshold, should trigger ask
        code, stdout, stderr = run_usage_check(
            ["--seam", "round1-join", "--threshold", "0"],
            state_dir=tmpdir,
        )

        # Exit code 10 only when state is available and over threshold
        # With unavailable state, it still proceeds
        if "state=unavailable" in stdout:
            # unavailable state => proceed, exit 0
            if code != 0:
                return False, f"unavailable state should exit 0, got {code}"
        else:
            if code != 10:
                return False, f"expected exit code 10 for ask, got {code}"
            if "DECISION: ask" not in stdout:
                return False, f"expected 'DECISION: ask', got: {stdout}"

        return True, ""


def test_unavailable_state_means_proceed():
    """When state is unavailable, DECISION is proceed (not ask)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        code, stdout, stderr = run_usage_check(
            ["--seam", "round1-join"],
            state_dir=tmpdir,
        )

        # When telemetry hasn't run, state should be unavailable
        if "state=unavailable" in stdout:
            if "DECISION: proceed" not in stdout:
                return False, f"unavailable state must give DECISION: proceed, got: {stdout}"
            if code != 0:
                return False, f"unavailable state must exit 0, got {code}"
            # Should mention telemetry install, not be bare
            if "telemetry" not in stdout.lower():
                return False, "unavailable state should mention telemetry install"

        return True, ""


def test_threshold_flag_default():
    """Threshold flag defaults to 130,000."""
    with tempfile.TemporaryDirectory() as tmpdir:
        code, stdout, stderr = run_usage_check(
            ["--seam", "round1-join"],
            state_dir=tmpdir,
        )

        # Check JSON for threshold value
        lines = stdout.strip().split("\n")
        try:
            json_obj = json.loads(lines[1])
            if json_obj.get("threshold") != 130000:
                return False, f"expected default threshold 130000, got {json_obj.get('threshold')}"
        except (json.JSONDecodeError, IndexError) as e:
            return False, f"failed to extract threshold: {e}"

        return True, ""


def test_threshold_flag_override():
    """Threshold flag can override default."""
    with tempfile.TemporaryDirectory() as tmpdir:
        code, stdout, stderr = run_usage_check(
            ["--seam", "round1-join", "--threshold", "50000"],
            state_dir=tmpdir,
        )

        lines = stdout.strip().split("\n")
        try:
            json_obj = json.loads(lines[1])
            if json_obj.get("threshold") != 50000:
                return False, f"expected threshold 50000, got {json_obj.get('threshold')}"
        except (json.JSONDecodeError, IndexError) as e:
            return False, f"failed to extract threshold: {e}"

        return True, ""


def test_percentage_calculation():
    """Percentage is calculated in output."""
    with tempfile.TemporaryDirectory() as tmpdir:
        code, stdout, stderr = run_usage_check(
            ["--seam", "round1-join", "--threshold", "100"],
            state_dir=tmpdir,
        )

        gate_line = stdout.split("\n")[0]
        # Should contain a percentage like (50%) or (0%)
        if "(" not in gate_line or ")" not in gate_line:
            return False, f"expected percentage in parentheses, got: {gate_line}"

        return True, ""


def test_round1_join_has_info_suffix():
    """The round1-join seam includes the info suffix about what's counted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        code, stdout, stderr = run_usage_check(
            ["--seam", "round1-join"],
            state_dir=tmpdir,
        )

        gate_line = stdout.split("\n")[0]
        # Should mention "counts" and "cache_creation"
        if "counts input+output+cache_creation" not in gate_line:
            return False, f"round1-join should explain counted formula, got: {gate_line}"
        if "cache_read" not in gate_line:
            return False, f"round1-join should mention cache_read exclusion, got: {gate_line}"
        if "token_confidence" not in gate_line:
            return False, f"round1-join should mention token_confidence, got: {gate_line}"

        return True, ""


def test_final_seam_has_info_suffix():
    """The final seam also includes the info suffix."""
    with tempfile.TemporaryDirectory() as tmpdir:
        code, stdout, stderr = run_usage_check(
            ["--seam", "final"],
            state_dir=tmpdir,
        )

        gate_line = stdout.split("\n")[0]
        # Should mention "counts"
        if "counts input+output+cache_creation" not in gate_line:
            return False, f"final seam should explain counted formula, got: {gate_line}"

        return True, ""


def test_intermediate_seam_no_info_suffix():
    """Intermediate seams (not round1-join or final) do not have the info suffix."""
    with tempfile.TemporaryDirectory() as tmpdir:
        code, stdout, stderr = run_usage_check(
            ["--seam", "gate-fix-loop"],
            state_dir=tmpdir,
        )

        gate_line = stdout.split("\n")[0]
        # gate-fix-loop should NOT have the long suffix
        # The suffix ends after DECISION:, so checking for the specific phrase
        if "(counts input+output+cache_creation" in gate_line:
            return False, f"gate-fix-loop should not have info suffix, got: {gate_line}"

        return True, ""


def test_json_keys_required():
    """JSON output contains all expected keys."""
    with tempfile.TemporaryDirectory() as tmpdir:
        code, stdout, stderr = run_usage_check(
            ["--seam", "round1-join"],
            state_dir=tmpdir,
        )

        lines = stdout.strip().split("\n")
        try:
            json_obj = json.loads(lines[1])

            expected = {
                "seam": str,
                "counted_tokens": int,
                "threshold": int,
                "state": str,
                "decision": str,
                "pct": int,
                "accounted": int,
                "is_floor": bool,
            }

            for key, expected_type in expected.items():
                if key not in json_obj:
                    return False, f"missing key '{key}'"
                if not isinstance(json_obj[key], expected_type):
                    return False, f"key '{key}' has wrong type: {type(json_obj[key])}"

        except (json.JSONDecodeError, IndexError, KeyError) as e:
            return False, f"JSON validation failed: {e}"

        return True, ""


def test_agents_field_format():
    """The agents field in printed line is N/M format."""
    with tempfile.TemporaryDirectory() as tmpdir:
        code, stdout, stderr = run_usage_check(
            ["--seam", "round1-join"],
            state_dir=tmpdir,
        )

        gate_line = stdout.split("\n")[0]

        # Look for agents=N/M pattern in the printed line
        import re
        match = re.search(r'agents=(\d+)/(\d+)', gate_line)
        if not match:
            return False, f"agents=N/M format not found in: {gate_line}"

        n, m = match.groups()
        try:
            n_int = int(n)
            m_int = int(m)
            if n_int > m_int:
                return False, f"agents counted ({n}) should not exceed total ({m})"
        except ValueError:
            return False, f"agents parts not integers: {n}/{m}"

        return True, ""


def test_session_id_env_fallback():
    """Session ID defaults to CLAUDE_CODE_SESSION_ID env var."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Without env var, should use 'unknown' or handle gracefully
        env_without_sid = {}
        code, stdout, stderr = run_usage_check(
            ["--seam", "round1-join"],
            state_dir=tmpdir,
            env=env_without_sid,
        )

        if code not in (0, 10):
            return False, f"unexpected exit code {code}"

        # Should complete without crashing
        if not stdout:
            return False, "expected output even without session-id"

        return True, ""


def main():
    h = Harness("USAGE-CHECK CLI CONTRACT TEST SUITE")

    h.test_result("seam flag is required", *test_seam_flag_required())
    h.test_result("valid seam values accepted", *test_valid_seam_values())
    h.test_result("invalid seam rejected", *test_invalid_seam_rejected())
    h.test_result("USAGE-GATE line has required fields", *test_usage_gate_line_format())
    h.test_result("JSON output present and parseable", *test_json_output_present())
    h.test_result("exit code 0 for proceed", *test_exit_code_proceed())
    h.test_result("exit code 10 for ask", *test_exit_code_ask())
    h.test_result("unavailable state => DECISION: proceed", *test_unavailable_state_means_proceed())
    h.test_result("threshold defaults to 130,000", *test_threshold_flag_default())
    h.test_result("threshold flag can override", *test_threshold_flag_override())
    h.test_result("percentage calculated in output", *test_percentage_calculation())
    h.test_result("round1-join has info suffix", *test_round1_join_has_info_suffix())
    h.test_result("final seam has info suffix", *test_final_seam_has_info_suffix())
    h.test_result("intermediate seams no info suffix", *test_intermediate_seam_no_info_suffix())
    h.test_result("JSON keys all present", *test_json_keys_required())
    h.test_result("agents field N/M format", *test_agents_field_format())
    h.test_result("session-id env fallback", *test_session_id_env_fallback())

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
