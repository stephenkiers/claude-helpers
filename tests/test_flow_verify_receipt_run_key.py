#!/usr/bin/env python3
"""
RECEIPT_CONTRACT and RUN_KEY parsing for implement/fix receipts (#346).

Tests the _parse_run_ref function and its integration into the verifier's flow.
Each OK implement/fix receipt test case runs through both the standard path and
--expect-model where practical.

Run with: python3 tests/test_flow_verify_receipt_run_key.py
"""

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

# Import the verifier module
script_path = Path(__file__).parent.parent / "scripts" / "flow-verify-receipt.py"
spec = importlib.util.spec_from_file_location("flow_verify_receipt", script_path)
flow_verify_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(flow_verify_module)

_parse_run_ref = flow_verify_module._parse_run_ref
RUN_KEY_RE = flow_verify_module.RUN_KEY_RE

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
SHA = "0000000000000000000000000000000000000001"
ISO_TIMESTAMP = "2026-10-20T10:00:00Z"


def run_verifier(args, home=None, cwd=None):
    """Run flow-verify-receipt.py and return (returncode, stdout, stderr)."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    if home:
        env["HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, str(VERIFIER)] + args,
        cwd=str(cwd or REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def make_receipt(
    decision="OK",
    include_receipt_contract=False,
    include_run_key=False,
    run_key_value="run-key-abc",
    include_outstanding=True,
    include_round1=True,
    malformed_run_key=False,
    duplicate_run_key=False,
    run_key_below_header=False,
    contract_value="1",
) -> str:
    """Generate a receipt for testing.

    Header block ends at the first blank line. Lines before the blank line are in header.
    Lines after the blank line are in content.
    """
    lines = []

    if decision == "OK":
        lines.append("ATTEMPT: 1")

        if include_round1:
            lines.append("ROUND 1 — Implementer (parallel)")

        # Add CHECK lines (minimal, required for OK receipts)
        lines.append("CHECK: pass")
        lines.append("CHECK_EXIT: 0")
        lines.append("CHECK_SOURCE: configured")
        lines.append("CHECK_SHA: 0000000000000000000000000000000000000001")

        # Add RECEIPT_CONTRACT and RUN_KEY in the header block
        if include_receipt_contract:
            if contract_value == "1":
                lines.append("RECEIPT_CONTRACT: 1")
            else:
                lines.append(f"RECEIPT_CONTRACT: {contract_value}")

        if include_run_key and not run_key_below_header:
            if malformed_run_key:
                lines.append("RUN_KEY: invalid key with spaces!")
            else:
                run_key_val = run_key_value
                if duplicate_run_key:
                    # Will add a second RUN_KEY line later
                    lines.append(f"RUN_KEY: {run_key_val}")
                    lines.append(f"RUN_KEY: {run_key_val}")
                else:
                    lines.append(f"RUN_KEY: {run_key_val}")

        if include_outstanding:
            lines.append("OUTSTANDING: none")

        lines.append("Decision: OK")
        lines.append("<!-- step-end -->")

        # Add content after step-end marker (below header)
        if run_key_below_header and include_run_key:
            if malformed_run_key:
                lines.append("RUN_KEY: invalid key with spaces!")
            else:
                lines.append(f"RUN_KEY: {run_key_value}")

    else:  # FAILED
        lines.append("ATTEMPT: 1")
        if include_receipt_contract:
            lines.append("RECEIPT_CONTRACT: 1")
        if include_run_key and not run_key_below_header:
            lines.append(f"RUN_KEY: {run_key_value}")
        lines.append("Decision: FAILED")
        lines.append("Reason: some reason")
        if run_key_below_header and include_run_key:
            lines.append(f"RUN_KEY: {run_key_value}")

    return "\n".join(lines) + "\n"


def test_pattern_equality(h):
    """Test that RUN_KEY_RE equals the one in implement-ledger.py."""
    try:
        import importlib.util
        ledger_path = Path(__file__).parent.parent / "scripts" / "implement-ledger.py"
        spec = importlib.util.spec_from_file_location("implement_ledger", ledger_path)
        ledger_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ledger_module)

        verifier_pattern = RUN_KEY_RE.pattern
        ledger_pattern = ledger_module.RUN_KEY_RE.pattern

        h.test_result(
            "RUN_KEY_RE pattern is equal between flow-verify-receipt.py and implement-ledger.py",
            verifier_pattern == ledger_pattern,
            f"verifier={verifier_pattern!r}, ledger={ledger_pattern!r}"
        )
    except Exception as e:
        h.test_result("Pattern equality check", False, str(e))


def test_parse_run_ref_present(h):
    """Test _parse_run_ref with a well-formed RUN_KEY."""
    receipt = make_receipt(include_receipt_contract=True, include_run_key=True)
    result = _parse_run_ref(receipt)
    h.test_result("Present: status=present with well-formed RUN_KEY",
                  result.status == "present" and result.key == "run-key-abc" and result.reason is None,
                  f"status={result.status}, key={result.key}, reason={result.reason}")


def test_parse_run_ref_key_without_marker(h):
    """Test _parse_run_ref with RUN_KEY but no marker."""
    receipt = make_receipt(include_receipt_contract=False, include_run_key=True)
    result = _parse_run_ref(receipt)
    h.test_result("Key without marker: status=present",
                  result.status == "present" and result.key == "run-key-abc",
                  f"status={result.status}, key={result.key}")


def test_parse_run_ref_grandfathered(h):
    """Test _parse_run_ref with neither line present."""
    receipt = make_receipt(include_receipt_contract=False, include_run_key=False)
    result = _parse_run_ref(receipt)
    h.test_result("Neither line present: status=grandfathered",
                  result.status == "grandfathered" and result.key is None and result.reason is None,
                  f"status={result.status}, key={result.key}, reason={result.reason}")


def test_parse_run_ref_marker_without_key(h):
    """Test _parse_run_ref with marker but no RUN_KEY."""
    receipt = make_receipt(include_receipt_contract=True, include_run_key=False)
    result = _parse_run_ref(receipt)
    h.test_result("Marker without key: status=invalid",
                  result.status == "invalid" and "receipt missing RUN_KEY" in result.reason,
                  f"status={result.status}, reason={result.reason}")


def test_parse_run_ref_malformed_key(h):
    """Test _parse_run_ref with malformed RUN_KEY."""
    receipt = make_receipt(include_receipt_contract=True, include_run_key=True, malformed_run_key=True)
    result = _parse_run_ref(receipt)
    h.test_result("Malformed key: status=invalid",
                  result.status == "invalid" and "malformed" in result.reason,
                  f"status={result.status}, reason={result.reason}")


def test_parse_run_ref_malformed_key_without_marker(h):
    """Test _parse_run_ref with malformed RUN_KEY but no marker."""
    receipt = make_receipt(include_receipt_contract=False, include_run_key=True, malformed_run_key=True)
    result = _parse_run_ref(receipt)
    h.test_result("Malformed key without marker: status=invalid",
                  result.status == "invalid" and "malformed" in result.reason,
                  f"status={result.status}, reason={result.reason}")


def test_parse_run_ref_duplicated_key(h):
    """Test _parse_run_ref with duplicated RUN_KEY."""
    receipt = make_receipt(include_receipt_contract=True, include_run_key=True, duplicate_run_key=True)
    result = _parse_run_ref(receipt)
    h.test_result("Duplicated RUN_KEY: status=invalid",
                  result.status == "invalid" and "duplicated" in result.reason,
                  f"status={result.status}, reason={result.reason}")


def test_parse_run_ref_key_below_header(h):
    """Test _parse_run_ref with RUN_KEY below the header block."""
    # This receipt has RUN_KEY after the blank line that ends the header
    receipt_text = "ATTEMPT: 1\nROUND 1\nCHECK: pass\nCHECK_EXIT: 0\nCHECK_SOURCE: configured\nCHECK_SHA: 0000000000000000000000000000000000000001\nRECEIPT_CONTRACT: 1\nOUTSTANDING: none\nDecision: OK\n\nRUN_KEY: run-key-abc\n"
    result = _parse_run_ref(receipt_text)
    h.test_result("RUN_KEY below header block: status=invalid",
                  result.status == "invalid" and "outside the header block" in result.reason,
                  f"status={result.status}, reason={result.reason}")


def test_parse_run_ref_unknown_contract_version(h):
    """Test _parse_run_ref with unknown RECEIPT_CONTRACT value."""
    receipt = make_receipt(include_receipt_contract=True, include_run_key=True, contract_value="2")
    result = _parse_run_ref(receipt)
    h.test_result("Unknown RECEIPT_CONTRACT version: status=invalid",
                  result.status == "invalid" and "unknown RECEIPT_CONTRACT" in result.reason,
                  f"status={result.status}, reason={result.reason}")




def test_verifier_integration_invalid_marker_no_key(h):
    """Test verifier CLI with marker but no key."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        receipt = home / "receipt.json"
        receipt_text = make_receipt(include_receipt_contract=True, include_run_key=False)
        receipt.write_text(receipt_text)

        rc, stdout, stderr = run_verifier(
            ["--step", "implement", "--receipt", str(receipt), "--since", ISO_TIMESTAMP,
             "--repo", str(REPO_ROOT), "--branch", "HEAD", "--base", SHA],
            home=home, cwd=home
        )
        h.test_result("Verifier: marker without key exits 1",
                      rc == 1, f"rc={rc}")
        h.test_result("Verifier: prints reason for marker without key",
                      "receipt missing RUN_KEY" in stdout,
                      f"stdout={stdout}")


def test_verifier_integration_malformed_key(h):
    """Test verifier CLI with malformed RUN_KEY."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        receipt = home / "receipt.json"
        receipt_text = make_receipt(include_receipt_contract=True, include_run_key=True,
                                   malformed_run_key=True)
        receipt.write_text(receipt_text)

        rc, stdout, stderr = run_verifier(
            ["--step", "implement", "--receipt", str(receipt), "--since", ISO_TIMESTAMP,
             "--repo", str(REPO_ROOT), "--branch", "HEAD", "--base", SHA],
            home=home, cwd=home
        )
        h.test_result("Verifier: malformed RUN_KEY exits 1",
                      rc == 1, f"rc={rc}")
        h.test_result("Verifier: prints reason for malformed key",
                      "malformed" in stdout,
                      f"stdout={stdout}")


def test_verifier_plan_receipt_no_run_key_output(h):
    """Test that plan receipts don't get RUN_KEY_SOURCE output."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        home.mkdir()
        flow_dir = home / ".claude" / "flows" / "repo" / "1-x-abc"
        (flow_dir / "steps").mkdir(parents=True)
        receipt = flow_dir / "steps" / "plan.json"

        # Plan receipt with ROUND 1 and OUTSTANDING (if needed)
        plan_receipt = "ATTEMPT: 1\nRound 1\nOUTSTANDING: none\nDecision: OK\n"
        receipt.write_text(plan_receipt)
        (flow_dir / "log.md").write_text("2026-10-20T10:00:00Z plan begin\n")

        rc, stdout, stderr = run_verifier(
            ["--step", "plan", "--receipt", str(receipt), "--since-from-log",
             "--flow-dir", str(flow_dir)],
            home=home, cwd=home
        )
        h.test_result("Verifier: plan receipt does not print RUN_KEY_SOURCE",
                      "RUN_KEY_SOURCE" not in stdout,
                      f"stdout={stdout}")


def main():
    h = Harness("RECEIPT_CONTRACT and RUN_KEY parsing (#346)")

    # Test pattern equality first
    test_pattern_equality(h)

    # Unit tests for _parse_run_ref
    test_parse_run_ref_present(h)
    test_parse_run_ref_key_without_marker(h)
    test_parse_run_ref_grandfathered(h)
    test_parse_run_ref_marker_without_key(h)
    test_parse_run_ref_malformed_key(h)
    test_parse_run_ref_malformed_key_without_marker(h)
    test_parse_run_ref_duplicated_key(h)
    test_parse_run_ref_key_below_header(h)
    test_parse_run_ref_unknown_contract_version(h)

    # Integration tests with verifier CLI
    test_verifier_integration_invalid_marker_no_key(h)
    test_verifier_integration_malformed_key(h)
    test_verifier_plan_receipt_no_run_key_output(h)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
