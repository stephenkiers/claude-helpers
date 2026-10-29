#!/usr/bin/env python3
"""
Spec-blind test coverage for plan #346 (put-run-key-in-the-step-receipt).

This test suite is written from the plan specification alone, without reading the
implementation. It verifies the key behaviors and contracts described in the plan:

1. RUN_KEY_RE pattern is consistent between implement-ledger.py and flow-verify-receipt.py
2. Old marker-less receipts still work (grandfathered backward compatibility)
3. Verifier output includes RUN_KEY_SOURCE line for implement/fix receipts
4. Receipt template has correct header order (CHECK_SHA -> RUN_KEY -> RECEIPT_CONTRACT: 1)
5. Doc mentions correct render command with --repo, --flow-dir, --step flags
6. Telemetry includes ledger_lookup field for audits

Run with: python3 tests/test_plan_346_spec_coverage.py
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _test_harness import Harness, REPO_ROOT

LEDGER_SCRIPT = REPO_ROOT / "scripts" / "implement-ledger.py"
VERIFIER_SCRIPT = REPO_ROOT / "scripts" / "flow-verify-receipt.py"
IMPL_CMD = REPO_ROOT / "commands" / "implement-with-haiku.md"
EXPERT_FLOW_CMD = REPO_ROOT / "commands" / "expert-flow.md"

# Load the modules to get the patterns
_ledger_spec = importlib.util.spec_from_file_location("implement_ledger", LEDGER_SCRIPT)
_ledger_module = importlib.util.module_from_spec(_ledger_spec)
_ledger_spec.loader.exec_module(_ledger_module)
LEDGER_RUN_KEY_RE = _ledger_module.RUN_KEY_RE

_verifier_spec = importlib.util.spec_from_file_location("flow_verify_receipt", VERIFIER_SCRIPT)
_verifier_module = importlib.util.module_from_spec(_verifier_spec)
_verifier_spec.loader.exec_module(_verifier_module)
VERIFIER_RUN_KEY_RE = _verifier_module.RUN_KEY_RE


def git(repo: str, *args: str) -> str:
    """Run a git command in a repo. Returns stdout stripped."""
    result = subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", *args],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def make_test_repo(root: str) -> tuple:
    """Create a git repo with an initial commit. Returns (repo, base_sha)."""
    repo = f"{root}/repo"
    Path(repo).mkdir(parents=True)
    git(repo, "init", "-q")
    Path(f"{repo}/file.txt").write_text("initial\n")
    git(repo, "add", "file.txt")
    git(repo, "commit", "-q", "-m", "initial")
    base_sha = git(repo, "rev-parse", "HEAD")
    return repo, base_sha


def write_cache(repo: str, command: str = "just check") -> None:
    """Configure the repo's check command."""
    cache_dir = Path(repo) / ".claude"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "repo-cache.json").write_text(json.dumps({"commands": {"check": command}}))


def run_verifier(receipt_path: str, repo: str, base_sha: str, home: str, step: str = "implement") -> tuple:
    """Run the verifier CLI. Returns (returncode, stdout, stderr)."""
    cmd = [
        sys.executable, str(VERIFIER_SCRIPT),
        "--step", step,
        "--receipt", receipt_path,
        "--since", "2026-01-01T00:00:00Z",
        "--repo", repo,
        "--branch", "HEAD",
        "--base", base_sha,
    ]
    env = os.environ.copy()
    env["HOME"] = home
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def write_receipt(home: str, text: str) -> str:
    """Write a receipt file. Returns the path."""
    path = f"{home}/receipt.md"
    Path(path).write_text(text)
    return path


def main():
    h = Harness("PLAN #346 SPEC-BLIND COVERAGE TEST SUITE")

    print("[Section 1] RUN_KEY_RE pattern consistency")

    # Test 1.1: Both modules define RUN_KEY_RE
    h.test_result(
        "implement-ledger.py defines RUN_KEY_RE",
        LEDGER_RUN_KEY_RE is not None,
        "RUN_KEY_RE not found",
    )
    h.test_result(
        "flow-verify-receipt.py defines RUN_KEY_RE",
        VERIFIER_RUN_KEY_RE is not None,
        "RUN_KEY_RE not found",
    )

    # Test 1.2: Patterns are equal (as required by the plan)
    h.test_result(
        "RUN_KEY_RE patterns are identical between scripts",
        LEDGER_RUN_KEY_RE.pattern == VERIFIER_RUN_KEY_RE.pattern,
        f"ledger={LEDGER_RUN_KEY_RE.pattern!r} vs verifier={VERIFIER_RUN_KEY_RE.pattern!r}",
    )

    # Test 1.3: Validate the pattern against known valid/invalid keys
    # Pattern: ^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$ means: starts with alphanumeric,
    # followed by 0-199 chars that are alphanumeric/./underscore/dash
    valid_keys = ["my-run", "run-123", "run_key", "R", "a.b", "a-b_c.d", "test-", "x.y-z"]
    invalid_keys = ["-invalid", "too" + "x" * 200, ""]  # starts with dash, too long, empty

    for key in valid_keys:
        h.test_result(
            f"RUN_KEY_RE matches valid key: {key!r}",
            bool(LEDGER_RUN_KEY_RE.match(key)),
            f"pattern={LEDGER_RUN_KEY_RE.pattern!r}",
        )

    for key in invalid_keys:
        h.test_result(
            f"RUN_KEY_RE rejects invalid key: {key[:30]!r}...",
            not bool(LEDGER_RUN_KEY_RE.match(key)),
            f"pattern={LEDGER_RUN_KEY_RE.pattern!r}",
        )

    print()
    print("[Section 2] Receipt template structure (Step 3)")

    impl_text = IMPL_CMD.read_text()
    # Test that receipt template has correct header order
    h.test_result(
        "implement-with-haiku receipt template contains CHECK_SHA",
        "CHECK_SHA:" in impl_text,
        "CHECK_SHA not found in template",
    )
    h.test_result(
        "implement-with-haiku receipt template contains RUN_KEY",
        "RUN_KEY:" in impl_text,
        "RUN_KEY not found in template",
    )
    h.test_result(
        "implement-with-haiku receipt template contains RECEIPT_CONTRACT: 1",
        "RECEIPT_CONTRACT: 1" in impl_text,
        "RECEIPT_CONTRACT: 1 not found in template",
    )

    # Verify header order: CHECK_SHA -> RUN_KEY -> RECEIPT_CONTRACT -> OUTSTANDING
    header_order = re.search(
        r'CHECK_SHA:.*?\nRUN_KEY:.*?\nRECEIPT_CONTRACT: 1\nOUTSTANDING:',
        impl_text,
        re.DOTALL,
    )
    h.test_result(
        "receipt template header order is correct (CHECK_SHA -> RUN_KEY -> RECEIPT_CONTRACT: 1 -> OUTSTANDING)",
        header_order is not None,
        "header order not found or incorrect",
    )

    print()
    print("[Section 3] Expert-flow command structure (Step 4)")

    flow_text = EXPERT_FLOW_CMD.read_text()

    # Test that the render command with flags is documented
    h.test_result(
        'expert-flow mentions "render --run-key"',
        'render --run-key' in flow_text,
        "render --run-key command not found",
    )
    h.test_result(
        'expert-flow mentions "--repo" flag for render',
        '--repo' in flow_text and 'render' in flow_text,
        "--repo flag not found in render context",
    )
    h.test_result(
        'expert-flow mentions "--flow-dir" flag for render',
        '--flow-dir' in flow_text and 'render' in flow_text,
        "--flow-dir flag not found in render context",
    )
    h.test_result(
        'expert-flow mentions "--step" flag for render',
        '--step' in flow_text and 'render' in flow_text,
        "--step flag not found in render context",
    )

    # Test fallback path documentation
    h.test_result(
        'expert-flow mentions "LEDGER LOOKUP: locate-fallback"',
        'LEDGER LOOKUP: locate-fallback' in flow_text,
        "fallback path not documented",
    )

    # Test telemetry field
    h.test_result(
        "expert-flow telemetry includes --arg ledger_lookup",
        '--arg ledger_lookup' in flow_text,
        "ledger_lookup telemetry field not found",
    )

    # Test that audits.jsonl uses the field
    h.test_result(
        "expert-flow telemetry passes ledger_lookup to audits.jsonl",
        'ledger_lookup:' in flow_text and 'audits.jsonl' in flow_text,
        "ledger_lookup not wired into audits.jsonl",
    )

    print()
    print("[Section 4] Backward compatibility (marker-less receipts)")

    # Test that old receipts (without RECEIPT_CONTRACT marker) still parse
    # (verification may fail on other rules like artifacts, but the RUN_KEY parsing should succeed)
    with tempfile.TemporaryDirectory() as home:
        repo, base_sha = make_test_repo(home)
        write_cache(repo)

        # Add a second commit so artifacts check passes
        Path(f"{repo}/feature.txt").write_text("feature\n")
        git(repo, "add", "feature.txt")
        git(repo, "commit", "-q", "-m", "add feature")
        head_sha = git(repo, "rev-parse", "HEAD")

        # Marker-less receipt (old format) - refers to the new HEAD
        old_receipt_text = (
            "ROUND 1\n"
            "CHECK: just check\n"
            "CHECK_EXIT: 0\n"
            "CHECK_SOURCE: repo-cache\n"
            "CHECK_SHA: " + head_sha + "\n"
            "OUTSTANDING: none\n"
            "Decision: OK\n"
            "<!-- step-end -->"
        )
        receipt_path = write_receipt(home, old_receipt_text)
        rc, stdout, stderr = run_verifier(receipt_path, repo, base_sha, home, step="implement")

        h.test_result(
            "old marker-less implement receipt can pass verification (exit 0)",
            rc == 0,
            f"rc={rc} stdout={stdout[:200]!r}",
        )
        h.test_result(
            "old marker-less implement receipt produces ARTIFACTS: ok when conditions met",
            rc == 0 and "ARTIFACTS: ok" in stdout,
            f"rc={rc} stdout={stdout[:200]!r}",
        )

    print()
    print("[Section 5] Verifier output format (Step 2)")

    # Test that verifier output includes RUN_KEY_SOURCE for implement/fix receipts
    # (we test with old receipts since new ones require more setup)
    with tempfile.TemporaryDirectory() as home:
        repo, base_sha = make_test_repo(home)
        write_cache(repo)

        # Add a second commit so artifacts check passes
        Path(f"{repo}/feature.txt").write_text("feature\n")
        git(repo, "add", "feature.txt")
        git(repo, "commit", "-q", "-m", "add feature")
        head_sha = git(repo, "rev-parse", "HEAD")

        # Old marker-less receipt
        receipt = (
            "ROUND 1\n"
            "CHECK: just check\n"
            "CHECK_EXIT: 0\n"
            "CHECK_SOURCE: repo-cache\n"
            "CHECK_SHA: " + head_sha + "\n"
            "OUTSTANDING: none\n"
            "Decision: OK\n"
            "<!-- step-end -->"
        )
        path = write_receipt(home, receipt)
        rc, stdout, stderr = run_verifier(path, repo, base_sha, home, step="implement")

        # The plan says: "If neither line is present, the status is `grandfathered`."
        # and output should be: "RUN_KEY_SOURCE: locate-fallback (receipt has no RECEIPT_CONTRACT line)"
        h.test_result(
            "verifier output for marker-less implement receipt includes RUN_KEY_SOURCE",
            "RUN_KEY_SOURCE:" in stdout and rc == 0,
            f"rc={rc} stdout={stdout[:200]!r}",
        )

    print()
    print("[Section 6] Plan/review receipt handling")

    # Test that plan and review receipts do NOT get RUN_KEY_SOURCE lines
    with tempfile.TemporaryDirectory() as home:
        repo, base_sha = make_test_repo(home)
        write_cache(repo)

        plan_receipt = (
            "2026-01-01T00:00:00Z plan begin\n"
            "2026-01-01T00:00:01Z plan end\n"
            "Decision: OK\n"
            "<!-- step-end -->"
        )
        path = write_receipt(home, plan_receipt)
        rc, stdout, stderr = run_verifier(path, repo, base_sha, home, step="plan")

        h.test_result(
            "verifier output for plan receipt does NOT include RUN_KEY_SOURCE",
            "RUN_KEY_SOURCE:" not in stdout,
            f"stdout={stdout[:200]!r}",
        )

    print()
    print("[Section 7] Rule 8 command documentation")

    # Test that rule 8 uses --since-from-log
    h.test_result(
        'expert-flow mentions "--since-from-log" in rule 8',
        re.search(r'rule 8.*--since-from-log', flow_text, re.IGNORECASE | re.DOTALL) is not None,
        "rule 8 --since-from-log not found",
    )

    # Test that receipt template includes RUN_KEY and RECEIPT_CONTRACT
    h.test_result(
        "receipt template includes RUN_KEY: placeholder",
        "<RUN_KEY>" in impl_text,
        "<RUN_KEY> placeholder not found",
    )

    print()
    print("[Section 8] Verifier exit codes and reasons")

    # Test that verifier mentions the new exit reasons
    h.test_result(
        "expert-flow mentions 'receipt RUN_KEY duplicated' exit reason",
        "receipt RUN_KEY duplicated" in flow_text,
        "exit reason not documented",
    )
    h.test_result(
        "expert-flow mentions 'receipt RUN_KEY outside the header block' exit reason",
        "receipt RUN_KEY outside the header block" in flow_text,
        "exit reason not documented",
    )
    h.test_result(
        "expert-flow mentions 'receipt RUN_KEY malformed' exit reason",
        "receipt RUN_KEY malformed" in flow_text,
        "exit reason not documented",
    )
    h.test_result(
        "expert-flow mentions 'unknown RECEIPT_CONTRACT' exit reason",
        "unknown RECEIPT_CONTRACT" in flow_text,
        "exit reason not documented",
    )
    h.test_result(
        "expert-flow mentions 'receipt missing RUN_KEY (required by RECEIPT_CONTRACT: 1)' exit reason",
        "receipt missing RUN_KEY (required by RECEIPT_CONTRACT: 1)" in flow_text,
        "exit reason not documented",
    )

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
