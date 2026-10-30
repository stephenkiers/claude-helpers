#!/usr/bin/env python3
"""
CLI-level near-miss handling for RECEIPT_CONTRACT and RUN_KEY lines in implement receipts (#346).

Driven through scripts/flow-verify-receipt.py (subprocess only). A line that is only a near-miss of the
strict column-0 `RUN_KEY:` / `RECEIPT_CONTRACT:` pattern (indented, bulleted, bolded, or mis-cased) must
be rejected as invalid (exit 1). It must never be treated as a pre-contract receipt and sent down the
locate-fallback path. The parser-level cases live in tests/test_flow_verify_receipt_run_key.py; this file
checks the verifier's exit code and printed output for each variant.

Covers:
1. Control: a receipt with no near-miss lines takes the grandfathered locate-fallback (exit 0).
2. Near-miss RUN_KEY variants (indented, bulleted, bold, lower-case, mixed-case): exit 1, no ARTIFACTS: ok,
   no locate-fallback on stdout.
3. Near-miss RECEIPT_CONTRACT variants (indented, bulleted, mis-cased): exit 1, no ARTIFACTS: ok,
   no locate-fallback on stdout.
4. A strict RUN_KEY alongside an indented RECEIPT_CONTRACT: exit 1 (the near-miss is not ignored).
5. A strict RECEIPT_CONTRACT alongside an indented RUN_KEY: exit 1 (the near-miss is not ignored).

Run with: python3 tests/test_flow_verify_receipt_near_miss.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import REPO_ROOT, Harness

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
SHA = "0000000000000000000000000000000000000001"
ISO_TIMESTAMP = "2026-10-20T10:00:00Z"
GIT_ID = ["-c", "user.email=test@example.com", "-c", "user.name=Test"]

RUN_KEY_NEAR_MISSES = (
    "  RUN_KEY: run-key-abc",
    "- RUN_KEY: run-key-abc",
    "* RUN_KEY: run-key-abc",
    "**RUN_KEY:** run-key-abc",
    "run_key: run-key-abc",
    "Run_Key: run-key-abc",
)

RECEIPT_CONTRACT_NEAR_MISSES = (
    "  RECEIPT_CONTRACT: 1",
    "- RECEIPT_CONTRACT: 1",
    "* RECEIPT_CONTRACT: 1",
    "receipt_contract: 1",
)


def run_verifier(args, home, cwd):
    """Run flow-verify-receipt.py under the given HOME. Returns (returncode, stdout, stderr)."""
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env["HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, str(VERIFIER)] + args,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


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


def make_receipt(check_sha, header_extra=(), strict_run_key=False, strict_contract=False):
    """An OK implement receipt whose header carries the given extra lines (near-miss or strict)."""
    lines = [
        "ATTEMPT: 1",
        "ROUND 1 — Implementer (parallel)",
        "CHECK: just check",
        "CHECK_EXIT: 0",
        "CHECK_SOURCE: repo-cache",
        f"CHECK_SHA: {check_sha}",
    ]
    if strict_contract:
        lines.append("RECEIPT_CONTRACT: 1")
    if strict_run_key:
        lines.append("RUN_KEY: run-key-abc")
    lines.extend(header_extra)
    lines.append("OUTSTANDING: none")
    lines.append("Decision: OK")
    lines.append("<!-- step-end -->")
    return "\n".join(lines) + "\n"


def verify_implement(home, receipt_text, repo, base):
    """Write receipt_text and run the implement verifier against repo. Returns (rc, stdout, stderr)."""
    receipt = Path(home) / "receipt.md"
    receipt.write_text(receipt_text)
    args = ["--step", "implement", "--receipt", str(receipt), "--since", ISO_TIMESTAMP,
            "--repo", str(repo), "--branch", "HEAD", "--base", base]
    return run_verifier(args, home=home, cwd=home)


def _rejected(stdout):
    """True when the verifier printed no success marker and no locate-fallback source line."""
    lines = stdout.splitlines()
    return "ARTIFACTS: ok" not in lines and not any("locate-fallback" in ln for ln in lines)


def test_control_grandfathered_receipt_passes(h):
    """Control: with no near-miss lines the pre-contract receipt takes the locate-fallback and exits 0."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        repo, base, head = make_feature_repo(home)
        rc, stdout, stderr = verify_implement(home, make_receipt(head), repo, base)
        h.test_result("control: pre-contract receipt exits 0 with ARTIFACTS: ok",
                      rc == 0 and "ARTIFACTS: ok" in stdout.splitlines(),
                      f"rc={rc} stdout={stdout!r} stderr={stderr!r}")
        h.test_result("control: pre-contract receipt reports locate-fallback",
                      any("RUN_KEY_SOURCE: locate-fallback" in ln for ln in stdout.splitlines()),
                      f"stdout={stdout!r}")


def test_run_key_near_miss_variants_exit_1(h):
    """Each near-miss RUN_KEY line (no strict lines at all) exits 1 and never takes the locate-fallback."""
    for variant in RUN_KEY_NEAR_MISSES:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            repo, base, head = make_feature_repo(home)
            rc, stdout, stderr = verify_implement(home, make_receipt(head, header_extra=(variant,)), repo, base)
            h.test_result(f"RUN_KEY near-miss {variant!r}: exits 1",
                          rc == 1, f"rc={rc} stdout={stdout!r} stderr={stderr!r}")
            h.test_result(f"RUN_KEY near-miss {variant!r}: no ARTIFACTS: ok and no locate-fallback",
                          _rejected(stdout), f"stdout={stdout!r}")


def test_receipt_contract_near_miss_variants_exit_1(h):
    """Each near-miss RECEIPT_CONTRACT line (no strict lines at all) exits 1 and never takes the locate-fallback."""
    for variant in RECEIPT_CONTRACT_NEAR_MISSES:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            repo, base, head = make_feature_repo(home)
            rc, stdout, stderr = verify_implement(home, make_receipt(head, header_extra=(variant,)), repo, base)
            h.test_result(f"RECEIPT_CONTRACT near-miss {variant!r}: exits 1",
                          rc == 1, f"rc={rc} stdout={stdout!r} stderr={stderr!r}")
            h.test_result(f"RECEIPT_CONTRACT near-miss {variant!r}: no ARTIFACTS: ok and no locate-fallback",
                          _rejected(stdout), f"stdout={stdout!r}")


def test_strict_run_key_with_indented_contract_exits_1(h):
    """A strict RUN_KEY does not excuse an indented RECEIPT_CONTRACT: exit 1, no ARTIFACTS: ok."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        repo, base, head = make_feature_repo(home)
        text = make_receipt(head, header_extra=("  RECEIPT_CONTRACT: 1",), strict_run_key=True)
        rc, stdout, stderr = verify_implement(home, text, repo, base)
        h.test_result("strict RUN_KEY + indented RECEIPT_CONTRACT: exits 1",
                      rc == 1 and "ARTIFACTS: ok" not in stdout.splitlines(),
                      f"rc={rc} stdout={stdout!r} stderr={stderr!r}")


def test_strict_contract_with_indented_run_key_exits_1(h):
    """A strict RECEIPT_CONTRACT does not excuse an indented RUN_KEY: exit 1, no ARTIFACTS: ok."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        repo, base, head = make_feature_repo(home)
        text = make_receipt(head, header_extra=("  RUN_KEY: run-key-abc",), strict_contract=True)
        rc, stdout, stderr = verify_implement(home, text, repo, base)
        h.test_result("strict RECEIPT_CONTRACT + indented RUN_KEY: exits 1",
                      rc == 1 and "ARTIFACTS: ok" not in stdout.splitlines(),
                      f"rc={rc} stdout={stdout!r} stderr={stderr!r}")


def main():
    h = Harness("flow-verify-receipt.py near-miss RECEIPT_CONTRACT / RUN_KEY lines (#346)")
    test_control_grandfathered_receipt_passes(h)
    test_run_key_near_miss_variants_exit_1(h)
    test_receipt_contract_near_miss_variants_exit_1(h)
    test_strict_run_key_with_indented_contract_exits_1(h)
    test_strict_contract_with_indented_run_key_exits_1(h)
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
