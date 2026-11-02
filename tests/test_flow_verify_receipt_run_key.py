#!/usr/bin/env python3
"""
RECEIPT_CONTRACT and RUN_KEY parsing for implement/fix receipts (#346).

Tests the _parse_run_ref function, the RunRef invariants, and the verifier CLI: the RUN_KEY_SOURCE
lines printed before ARTIFACTS: ok (receipt and locate-fallback, on both the standard and the
--expect-model path), the rejection reasons (unknown contract, duplicated, outside the header, malformed,
missing, near-miss), and that plan receipts stay unaffected.

Run with: python3 tests/test_flow_verify_receipt_run_key.py
"""

import importlib.util
import json
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
RunRef = flow_verify_module.RunRef
RUN_KEY_RE = flow_verify_module.RUN_KEY_RE

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
SHA = "0000000000000000000000000000000000000001"
ISO_TIMESTAMP = "2026-10-20T10:00:00Z"
GIT_ID = ["-c", "user.email=test@example.com", "-c", "user.name=Test"]
EXPECT_MODEL_ARGS = ("--expect-model", "haiku")
EXPECT_MODEL_PREFIX = "IMPLEMENTER_MODEL: haiku\n"


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
    check_sha=SHA,
    check_command="just check",
    check_source="repo-cache",
) -> str:
    """Generate a receipt for testing.

    Header block ends at the first blank line or the step-end marker. Lines before it are in header.
    Lines after it are in content.
    """
    lines = []

    if decision == "OK":
        lines.append("ATTEMPT: 1")

        if include_round1:
            lines.append("ROUND 1 — Implementer (parallel)")

        # Add CHECK lines (minimal, required for OK receipts)
        lines.append(f"CHECK: {check_command}")
        lines.append("CHECK_EXIT: 0")
        lines.append(f"CHECK_SOURCE: {check_source}")
        lines.append(f"CHECK_SHA: {check_sha}")

        # Add RECEIPT_CONTRACT and RUN_KEY in the header block
        if include_receipt_contract:
            lines.append(f"RECEIPT_CONTRACT: {contract_value}")

        if include_run_key and not run_key_below_header:
            if malformed_run_key:
                lines.append("RUN_KEY: invalid key with spaces!")
            else:
                lines.append(f"RUN_KEY: {run_key_value}")
                if duplicate_run_key:
                    lines.append(f"RUN_KEY: {run_key_value}")

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


def git(repo, *args):
    """Run git in repo with a fixed identity. Returns stdout stripped."""
    result = subprocess.run(["git", *GIT_ID, *args], cwd=str(repo), capture_output=True, text=True)
    return result.stdout.strip()


def make_feature_repo(home):
    """A repo with a base commit and one feature commit, and a `just check` cache. Returns (repo, base, head)."""
    repo = Path(home) / "repo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    (repo / "initial.txt").write_text("initial\n")
    git(repo, "add", "initial.txt")
    git(repo, "commit", "-q", "-m", "initial")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "feature.txt").write_text("feature\n")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-q", "-m", "add feature")
    head = git(repo, "rev-parse", "HEAD")
    cache_dir = repo / ".claude"
    cache_dir.mkdir()
    (cache_dir / "repo-cache.json").write_text(json.dumps({"commands": {"check": "just check"}}))
    return repo, base, head


def verify_implement(home, receipt_text, repo, base, extra=()):
    """Write receipt_text and run the implement verifier against repo. Returns (rc, stdout, stderr)."""
    receipt = Path(home) / "receipt.md"
    receipt.write_text(receipt_text)
    args = ["--step", "implement", "--receipt", str(receipt), "--since", ISO_TIMESTAMP,
            "--repo", str(repo), "--branch", "HEAD", "--base", base, *extra]
    return run_verifier(args, home=home, cwd=home)


def make_plan_fixture(home):
    """A plan receipt that passes check_plan: plan file, session dir with a contributor-free roster."""
    flow_dir = Path(home) / ".claude" / "flows" / "repo" / "1-x-abc"
    (flow_dir / "steps").mkdir(parents=True)
    (flow_dir / "log.md").write_text("2026-10-20T10:00:00Z plan begin\n")
    plan_file = Path(home) / ".claude" / "plans" / "slug-20260101T000000-00001.md"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text("# plan\n")
    session = Path(home) / ".claude" / "plan-sessions" / "repo" / "slug-20260101T000000-00001"
    session.mkdir(parents=True)
    (session / "context.md").write_text("context\n")
    (session / "selected-experts.md").write_text("| Expert | Role |\n| --- | --- |\n| Auditor | audit |\n")
    (session / "plan.md").write_text("# plan\n")
    (session / "decisions.md").write_text("# decisions\n")
    receipt = flow_dir / "steps" / "plan.json"
    receipt.write_text(f"FINAL_PLAN_PATH: {plan_file}\nATTEMPT: 1\nDecision: OK\n<!-- step-end -->\n")
    return flow_dir, receipt


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


def test_parse_run_ref_near_miss(h):
    """Tolerant-pattern near-misses fail closed as invalid, never the grandfathered fallback (#346)."""
    variants = (
        "- RUN_KEY: run-key-abc",
        "  RUN_KEY: run-key-abc",
        "* RUN_KEY: run-key-abc",
        "**RUN_KEY:** run-key-abc",
        "run_key: run-key-abc",
        "RUN_KEY : run-key-abc",
        "> RECEIPT_CONTRACT: 1",
    )
    for variant in variants:
        # No strict lines at all: without the near-miss rule this receipt would be grandfathered.
        receipt = make_receipt(include_receipt_contract=False, include_run_key=False)
        receipt = receipt.replace("Decision: OK", f"{variant}\nDecision: OK")
        result = _parse_run_ref(receipt)
        h.test_result(f"Near-miss {variant!r}: status=invalid with a near-miss reason",
                      result.status == "invalid" and result.reason is not None and "near-miss" in result.reason,
                      f"status={result.status}, reason={result.reason}")


def test_parse_run_ref_prose_is_not_near_miss(h):
    """A sentence that merely mentions RUN_KEY is not a near-miss and keeps the grandfathered fallback."""
    receipt = make_receipt(include_receipt_contract=False, include_run_key=False)
    receipt = receipt.replace("Decision: OK", "The RUN_KEY: field is documented elsewhere\nDecision: OK")
    result = _parse_run_ref(receipt)
    h.test_result("Prose mention of RUN_KEY: status=grandfathered",
                  result.status == "grandfathered" and result.reason is None,
                  f"status={result.status}, reason={result.reason}")


def test_run_ref_rejects_illegal_states(h):
    """RunRef refuses field combinations that no verdict path can produce (#346)."""
    legal = (
        dict(status="present", key="run-key-abc", reason=None),
        dict(status="grandfathered", key=None, reason=None),
        dict(status="invalid", key=None, reason="receipt RUN_KEY malformed"),
    )
    illegal = (
        dict(status="present", key=None, reason=None),
        dict(status="present", key="bad key", reason=None),
        dict(status="present", key="run-key-abc", reason="stray"),
        dict(status="grandfathered", key="run-key-abc", reason=None),
        dict(status="grandfathered", key=None, reason="stray"),
        dict(status="invalid", key=None, reason=None),
        dict(status="invalid", key="run-key-abc", reason="stray"),
    )
    for kwargs in legal:
        try:
            RunRef(**kwargs)
            ok = True
        except ValueError:
            ok = False
        h.test_result(f"RunRef accepts legal state {kwargs['status']!r}", ok, f"kwargs={kwargs}")
    for kwargs in illegal:
        try:
            RunRef(**kwargs)
            rejected = False
        except ValueError:
            rejected = True
        h.test_result(f"RunRef rejects illegal state {kwargs}", rejected, f"kwargs={kwargs}")


def test_verifier_ok_receipt_source_lines(h):
    """An OK receipt with a well-formed RUN_KEY prints RUN_KEY_SOURCE and RUN_KEY right before ARTIFACTS: ok."""
    modes = (("standard", (), lambda text: text), ("expect-model", EXPECT_MODEL_ARGS, lambda text: EXPECT_MODEL_PREFIX + text))
    for label, extra, wrap in modes:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            repo, base, head = make_feature_repo(home)
            text = wrap(make_receipt(include_receipt_contract=True, include_run_key=True, check_sha=head))
            rc, stdout, stderr = verify_implement(home, text, repo, base, extra)
            lines = stdout.splitlines()
            h.test_result(f"{label}: OK receipt with RECEIPT_CONTRACT and RUN_KEY exits 0",
                          rc == 0, f"rc={rc} stdout={stdout!r} stderr={stderr!r}")
            h.test_result(f"{label}: prints RUN_KEY_SOURCE: receipt and RUN_KEY before ARTIFACTS: ok",
                          lines[-3:] == ["RUN_KEY_SOURCE: receipt", "RUN_KEY: run-key-abc", "ARTIFACTS: ok"],
                          f"stdout={stdout!r}")


def test_verifier_ok_receipt_locate_fallback(h):
    """An OK receipt with neither line takes the locate-fallback, on both paths (#346)."""
    modes = (("standard", (), lambda text: text), ("expect-model", EXPECT_MODEL_ARGS, lambda text: EXPECT_MODEL_PREFIX + text))
    for label, extra, wrap in modes:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            repo, base, head = make_feature_repo(home)
            text = wrap(make_receipt(include_receipt_contract=False, include_run_key=False, check_sha=head))
            rc, stdout, stderr = verify_implement(home, text, repo, base, extra)
            lines = stdout.splitlines()
            h.test_result(f"{label}: grandfathered OK receipt exits 0",
                          rc == 0, f"rc={rc} stdout={stdout!r} stderr={stderr!r}")
            h.test_result(f"{label}: prints RUN_KEY_SOURCE: locate-fallback before ARTIFACTS: ok",
                          lines[-2:] != [] and lines[-2].startswith("RUN_KEY_SOURCE: locate-fallback")
                          and lines[-1] == "ARTIFACTS: ok",
                          f"stdout={stdout!r}")


def test_verifier_rejections(h):
    """Each invalid header shape exits 1 with its Reason and never prints ARTIFACTS: ok."""
    cases = (
        ("unknown RECEIPT_CONTRACT",
         dict(include_receipt_contract=True, include_run_key=True, contract_value="2"),
         "Reason: unknown RECEIPT_CONTRACT '2'"),
        ("duplicated RUN_KEY",
         dict(include_receipt_contract=True, include_run_key=True, duplicate_run_key=True),
         "Reason: receipt RUN_KEY duplicated"),
        ("RUN_KEY below the header",
         dict(include_receipt_contract=True, include_run_key=True, run_key_below_header=True),
         "Reason: receipt RUN_KEY outside the header block"),
        ("malformed RUN_KEY",
         dict(include_receipt_contract=True, include_run_key=True, malformed_run_key=True),
         "Reason: receipt RUN_KEY malformed"),
        ("marker without RUN_KEY",
         dict(include_receipt_contract=True, include_run_key=False),
         "Reason: receipt missing RUN_KEY (required by RECEIPT_CONTRACT: 1)"),
    )
    for label, kwargs, reason in cases:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            repo, base, head = make_feature_repo(home)
            text = make_receipt(check_sha=head, **kwargs)
            rc, stdout, stderr = verify_implement(home, text, repo, base)
            lines = stdout.splitlines()
            h.test_result(f"Verifier: {label} exits 1 with its Reason",
                          rc == 1 and reason in lines, f"rc={rc} stdout={stdout!r}")
            h.test_result(f"Verifier: {label} prints no RUN_KEY_SOURCE or ARTIFACTS: ok",
                          not any(ln.startswith("RUN_KEY_SOURCE") for ln in lines) and "ARTIFACTS: ok" not in lines,
                          f"stdout={stdout!r}")


def test_verifier_near_miss_rejected(h):
    """A near-miss RUN_KEY line exits 1 instead of taking the locate-fallback (#346)."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        repo, base, head = make_feature_repo(home)
        text = make_receipt(include_receipt_contract=False, include_run_key=False, check_sha=head)
        text = text.replace("Decision: OK", "- RUN_KEY: run-key-abc\nDecision: OK")
        rc, stdout, stderr = verify_implement(home, text, repo, base)
        lines = stdout.splitlines()
        h.test_result("Verifier: indented or bulleted RUN_KEY exits 1",
                      rc == 1, f"rc={rc} stdout={stdout!r}")
        h.test_result("Verifier: near-miss gets a near-miss Reason and no locate-fallback",
                      any(ln.startswith("Reason: receipt RUN_KEY near-miss") for ln in lines)
                      and not any("locate-fallback" in ln for ln in lines),
                      f"stdout={stdout!r}")


def test_verifier_plan_receipt_no_run_key_output(h):
    """A passing plan receipt exits 0 and never prints RUN_KEY_SOURCE (#346: plan receipts are unaffected)."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        flow_dir, receipt = make_plan_fixture(home)
        rc, stdout, stderr = run_verifier(
            ["--step", "plan", "--receipt", str(receipt), "--since-from-log",
             "--flow-dir", str(flow_dir)],
            home=home, cwd=home
        )
        h.test_result("Verifier: passing plan receipt exits 0 with ARTIFACTS: ok",
                      rc == 0 and "ARTIFACTS: ok" in stdout.splitlines(),
                      f"rc={rc} stdout={stdout!r} stderr={stderr!r}")
        h.test_result("Verifier: plan receipt does not print RUN_KEY_SOURCE",
                      "RUN_KEY_SOURCE" not in stdout,
                      f"stdout={stdout!r}")


def main():
    h = Harness("RECEIPT_CONTRACT and RUN_KEY parsing (#346)")

    # Test pattern equality first
    test_pattern_equality(h)

    # Unit tests for _parse_run_ref and RunRef
    test_parse_run_ref_present(h)
    test_parse_run_ref_key_without_marker(h)
    test_parse_run_ref_grandfathered(h)
    test_parse_run_ref_marker_without_key(h)
    test_parse_run_ref_malformed_key(h)
    test_parse_run_ref_malformed_key_without_marker(h)
    test_parse_run_ref_duplicated_key(h)
    test_parse_run_ref_key_below_header(h)
    test_parse_run_ref_unknown_contract_version(h)
    test_parse_run_ref_near_miss(h)
    test_parse_run_ref_prose_is_not_near_miss(h)
    test_run_ref_rejects_illegal_states(h)

    # Integration tests with verifier CLI
    test_verifier_ok_receipt_source_lines(h)
    test_verifier_ok_receipt_locate_fallback(h)
    test_verifier_rejections(h)
    test_verifier_near_miss_rejected(h)
    test_verifier_plan_receipt_no_run_key_output(h)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
