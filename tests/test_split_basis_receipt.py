#!/usr/bin/env python3
"""
Tests for SPLIT_BASIS receipt field validation in scripts/flow-verify-receipt.py.

Tests verify:
- Legal SPLIT_BASIS values: parallel, sequential, single(<reason>), none
- MIN_UNITS: int or "unknown"
- PRIOR_SPLIT_BASIS: basis value or none
- UNITS: int on OK receipts, required when basis is not none on FAILED receipts
- OK receipts require all three fields with real (non-none) basis
- FAILED receipts written before split decide use SPLIT_BASIS: none sentinel
- FAILED receipts with absent fields are accepted (lenient for old writers)
- Reason parsing handles ) in the reason text (takes everything between first ( and last ))
- sequential with UNITS: 1 is rejected
- single with MIN_UNITS: unknown is accepted
- single with MIN_UNITS > 1 (known) is rejected
- UNITS < MIN_UNITS is rejected (when both present)

Run with: python3 tests/test_split_basis_receipt.py
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"


def create_git_repo(repo_path: str) -> None:
    """Create a minimal git repo with one commit."""
    Path(repo_path).mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_path, capture_output=True)
    Path(f"{repo_path}/file.txt").write_text("initial")
    subprocess.run(["git", "add", "file.txt"], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_path, capture_output=True)


def get_head_sha(repo_path: str) -> str:
    """Get the current HEAD SHA."""
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True)
    return result.stdout.strip()


def run_verifier(step: str, receipt_path: str, since: str, env_home: str, **kwargs) -> tuple:
    """Run the verifier and return (exit_code, stdout, stderr)."""
    cmd = [
        sys.executable,
        str(VERIFIER),
        "--step", step,
        "--receipt", receipt_path,
        "--since", since,
    ]
    for key, value in kwargs.items():
        if value is not None:
            arg_name = key.replace("_", "-")
            cmd.extend([f"--{arg_name}", str(value)])

    env = os.environ.copy()
    env["HOME"] = env_home
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def main() -> None:
    h = Harness("SPLIT_BASIS receipt field validation")

    with tempfile.TemporaryDirectory() as tmp:
        print("[Test 1] OK receipt with valid parallel basis and all fields passes")
        repo_path = f"{tmp}/repo1"
        create_git_repo(repo_path)
        base_sha = get_head_sha(repo_path)
        # Make a new commit to have something to verify
        Path(f"{repo_path}/file2.txt").write_text("new")
        subprocess.run(["git", "add", "file2.txt"], cwd=repo_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add file2"], cwd=repo_path, capture_output=True)
        head_sha = get_head_sha(repo_path)

        receipt_path = f"{tmp}/receipt1.md"
        Path(receipt_path).write_text(
            "CHECK: just check\nCHECK_EXIT: 0\nCHECK_SOURCE: repo-cache\n"
            "CHECK_SHA: " + head_sha + "\n"
            "SPLIT_BASIS: parallel\nMIN_UNITS: 2\nPRIOR_SPLIT_BASIS: none\nUNITS: 2\n"
            "OUTSTANDING: none\nDecision: OK\n<!-- step-end -->"
        )
        # Write a repo-cache.json for the check
        cache_dir = Path(repo_path) / ".claude"
        cache_dir.mkdir(exist_ok=True)
        import json
        (cache_dir / "repo-cache.json").write_text(json.dumps({"commands": {"check": "just check"}}))

        code, out, err = run_verifier("implement", receipt_path, "2026-10-13T00:00:00Z", tmp, repo=repo_path, branch="HEAD", base=base_sha)
        h.test_result("OK parallel passes", code == 0, f"exit {code}: {err}")

        print("\n[Test 2] OK receipt with sequential basis and valid UNITS passes")
        receipt_path = f"{tmp}/receipt2.md"
        Path(receipt_path).write_text(
            "CHECK: just check\nCHECK_EXIT: 0\nCHECK_SOURCE: repo-cache\n"
            "CHECK_SHA: " + base_sha + "\n"
            "SPLIT_BASIS: sequential\nMIN_UNITS: 3\nPRIOR_SPLIT_BASIS: parallel\nUNITS: 3\n"
            "OUTSTANDING: none\nDecision: OK\n<!-- step-end -->"
        )
        code, _, err = run_verifier("implement", receipt_path, "2026-10-13T00:00:00Z", tmp, repo=repo_path, branch="HEAD", base=base_sha)
        h.test_result("OK sequential passes", code == 0, f"exit {code}: {err}")

        print("\n[Test 3] OK receipt with single basis passes")
        receipt_path = f"{tmp}/receipt3.md"
        Path(receipt_path).write_text(
            "CHECK: just check\nCHECK_EXIT: 0\nCHECK_SOURCE: repo-cache\n"
            "CHECK_SHA: " + base_sha + "\n"
            "SPLIT_BASIS: single(coupled work)\nMIN_UNITS: unknown\nPRIOR_SPLIT_BASIS: none\nUNITS: 1\n"
            "OUTSTANDING: none\nDecision: OK\n<!-- step-end -->"
        )
        code, _, err = run_verifier("implement", receipt_path, "2026-10-13T00:00:00Z", tmp, repo=repo_path, branch="HEAD", base=base_sha)
        h.test_result("OK single passes", code == 0, f"exit {code}: {err}")

        print("\n[Test 4] OK receipt with SPLIT_BASIS: none is rejected")
        receipt_path = f"{tmp}/receipt4.md"
        Path(receipt_path).write_text(
            "CHECK: just check\nCHECK_EXIT: 0\nCHECK_SOURCE: repo-cache\n"
            "CHECK_SHA: " + base_sha + "\n"
            "SPLIT_BASIS: none\nMIN_UNITS: 1\nPRIOR_SPLIT_BASIS: none\nUNITS: 1\n"
            "OUTSTANDING: none\nDecision: OK\n<!-- step-end -->"
        )
        code, _, err = run_verifier("implement", receipt_path, "2026-10-13T00:00:00Z", tmp, repo=repo_path, branch="HEAD", base=base_sha)
        h.test_result("OK with none basis rejected", code != 0, f"exit {code}")

        print("\n[Test 5] OK receipt missing split_basis fields is rejected")
        receipt_path = f"{tmp}/receipt5.md"
        Path(receipt_path).write_text(
            "CHECK: just check\nCHECK_EXIT: 0\nCHECK_SOURCE: repo-cache\n"
            "CHECK_SHA: " + base_sha + "\n"
            "OUTSTANDING: none\nDecision: OK\n<!-- step-end -->"
        )
        code, _, err = run_verifier("implement", receipt_path, "2026-10-13T00:00:00Z", tmp, repo=repo_path, branch="HEAD", base=base_sha)
        h.test_result("OK missing split fields rejected", code != 0 and ("missing" in err or "split" in err.lower()), err)

        print("\n[Test 6] FAILED receipt with SPLIT_BASIS: none (pre-split) passes")
        receipt_path = f"{tmp}/receipt6.md"
        Path(receipt_path).write_text(
            "Decision: FAILED\nReason: pre-split error\n"
            "FAILURE_CLASS: other\nSPLIT_BASIS: none\nMIN_UNITS: unknown\nPRIOR_SPLIT_BASIS: none\n"
            "IMPLEMENTER_MODEL: haiku\nUNITS: 1\nTURNS_USED: unknown\nMAX_TURNS: 40\nTURNS_EXHAUSTED: unknown\n"
            "<!-- step-end -->"
        )
        code, _, _ = run_verifier("implement", receipt_path, "2026-10-13T00:00:00Z", tmp)
        h.test_result("FAILED with none basis passes", code == 0, f"exit {code}")

        print("\n[Test 7] FAILED receipt with real basis and UNITS passes")
        receipt_path = f"{tmp}/receipt7.md"
        Path(receipt_path).write_text(
            "Decision: FAILED\nReason: build failure\n"
            "FAILURE_CLASS: other\nSPLIT_BASIS: sequential\nMIN_UNITS: 2\nPRIOR_SPLIT_BASIS: none\n"
            "IMPLEMENTER_MODEL: sonnet\nUNITS: 2\nTURNS_USED: unknown\nMAX_TURNS: 40\nTURNS_EXHAUSTED: unknown\n"
            "<!-- step-end -->"
        )
        code, _, _ = run_verifier("implement", receipt_path, "2026-10-13T00:00:00Z", tmp)
        h.test_result("FAILED with real basis passes", code == 0, f"exit {code}")

        print("\n[Test 8] FAILED receipt without split fields (old writer) passes (lenient)")
        receipt_path = f"{tmp}/receipt8.md"
        Path(receipt_path).write_text(
            "Decision: FAILED\nReason: old error\n"
            "FAILURE_CLASS: other\nIMPLEMENTER_MODEL: haiku\nUNITS: 1\nTURNS_USED: unknown\n"
            "<!-- step-end -->"
        )
        code, _, _ = run_verifier("implement", receipt_path, "2026-10-13T00:00:00Z", tmp)
        h.test_result("FAILED without split fields passes", code == 0, f"exit {code}")

        print("\n[Test 9] sequential with UNITS: 1 is rejected")
        receipt_path = f"{tmp}/receipt9.md"
        Path(receipt_path).write_text(
            "Decision: FAILED\nReason: bad\n"
            "FAILURE_CLASS: other\nSPLIT_BASIS: sequential\nMIN_UNITS: 1\nPRIOR_SPLIT_BASIS: none\n"
            "IMPLEMENTER_MODEL: haiku\nUNITS: 1\nTURNS_USED: unknown\n"
            "<!-- step-end -->"
        )
        code, _, err = run_verifier("implement", receipt_path, "2026-10-13T00:00:00Z", tmp)
        h.test_result("sequential with UNITS: 1 rejected", code != 0, f"exit {code}")

        print("\n[Test 10] single with MIN_UNITS: unknown and reason passes")
        receipt_path = f"{tmp}/receipt10.md"
        Path(receipt_path).write_text(
            "Decision: FAILED\nReason: primary-checkout-drift: a.py\n"
            "FAILURE_CLASS: cwd-drift\nSPLIT_BASIS: single(unknown)\nMIN_UNITS: unknown\nPRIOR_SPLIT_BASIS: none\n"
            "IMPLEMENTER_MODEL: haiku\nUNITS: 1\nTURNS_USED: unknown\n"
            "<!-- step-end -->"
        )
        code, _, _ = run_verifier("implement", receipt_path, "2026-10-13T00:00:00Z", tmp)
        h.test_result("single with unknown and reason passes", code == 0, f"exit {code}")

        print("\n[Test 11] single with MIN_UNITS: 2 (known, >1) is rejected")
        receipt_path = f"{tmp}/receipt11.md"
        Path(receipt_path).write_text(
            "Decision: FAILED\nReason: error\n"
            "FAILURE_CLASS: other\nSPLIT_BASIS: single(coupled)\nMIN_UNITS: 2\nPRIOR_SPLIT_BASIS: none\n"
            "IMPLEMENTER_MODEL: haiku\nUNITS: 1\nTURNS_USED: unknown\n"
            "<!-- step-end -->"
        )
        code, _, err = run_verifier("implement", receipt_path, "2026-10-13T00:00:00Z", tmp)
        h.test_result("single with MIN_UNITS: 2 rejected", code != 0, f"exit {code}")

        print("\n[Test 12] UNITS < MIN_UNITS is rejected")
        receipt_path = f"{tmp}/receipt12.md"
        Path(receipt_path).write_text(
            "Decision: FAILED\nReason: error\n"
            "FAILURE_CLASS: other\nSPLIT_BASIS: parallel\nMIN_UNITS: 3\nPRIOR_SPLIT_BASIS: none\n"
            "IMPLEMENTER_MODEL: haiku\nUNITS: 2\nTURNS_USED: unknown\n"
            "<!-- step-end -->"
        )
        code, _, err = run_verifier("implement", receipt_path, "2026-10-13T00:00:00Z", tmp)
        h.test_result("UNITS < MIN_UNITS rejected", code != 0, f"exit {code}")

        print("\n[Test 13] Reason parsing handles ) in the text (takes last ) on the line)")
        receipt_path = f"{tmp}/receipt13.md"
        Path(receipt_path).write_text(
            "Decision: OK\n"
            "CHECK: just check\nCHECK_EXIT: 0\nCHECK_SOURCE: repo-cache\n"
            "CHECK_SHA: " + base_sha + "\n"
            "SPLIT_BASIS: single(reason with ) paren)\nMIN_UNITS: unknown\nPRIOR_SPLIT_BASIS: none\nUNITS: 1\n"
            "OUTSTANDING: none\n<!-- step-end -->"
        )
        code, out, err = run_verifier("implement", receipt_path, "2026-10-13T00:00:00Z", tmp, repo=repo_path, branch="HEAD", base=base_sha)
        h.test_result("reason with ) parsed correctly", code == 0, f"exit {code}: {err}")

        print("\n[Test 14] single with empty reason is rejected")
        receipt_path = f"{tmp}/receipt14.md"
        Path(receipt_path).write_text(
            "Decision: FAILED\nReason: error\n"
            "FAILURE_CLASS: other\nSPLIT_BASIS: single()\nMIN_UNITS: unknown\nPRIOR_SPLIT_BASIS: none\n"
            "IMPLEMENTER_MODEL: haiku\nUNITS: 1\nTURNS_USED: unknown\n"
            "<!-- step-end -->"
        )
        code, _, err = run_verifier("implement", receipt_path, "2026-10-13T00:00:00Z", tmp)
        h.test_result("single with empty reason rejected", code != 0, f"exit {code}")

        print("\n[Test 15] PRIOR_SPLIT_BASIS: sequential is accepted on retry")
        receipt_path = f"{tmp}/receipt15.md"
        Path(receipt_path).write_text(
            "CHECK: just check\nCHECK_EXIT: 0\nCHECK_SOURCE: repo-cache\n"
            "CHECK_SHA: " + base_sha + "\n"
            "SPLIT_BASIS: parallel\nMIN_UNITS: 2\nPRIOR_SPLIT_BASIS: sequential\nUNITS: 3\n"
            "OUTSTANDING: none\nDecision: OK\n<!-- step-end -->"
        )
        code, _, _ = run_verifier("implement", receipt_path, "2026-10-13T00:00:00Z", tmp, repo=repo_path, branch="HEAD", base=base_sha)
        h.test_result("PRIOR_SPLIT_BASIS set on retry passes", code == 0, f"exit {code}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
