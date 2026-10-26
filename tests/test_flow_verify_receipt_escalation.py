#!/usr/bin/env python3
"""
Escalation-mode tests for scripts/flow-verify-receipt.py

Tests verify the new --escalation flag and its seven-condition bounded path,
--expect-model checks, and discarded-file removal validation.

Run with: python3 tests/test_flow_verify_receipt_escalation.py
"""

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


def run_verifier(step: str, receipt_path: str, escalation: bool = True, **kwargs) -> tuple:
    """Run the verifier in escalation mode (or expect-model mode) and return (exit_code, stdout, stderr)."""
    cmd = [
        sys.executable,
        str(VERIFIER),
        "--step", step,
        "--receipt", receipt_path,
    ]
    if escalation:
        cmd.append("--escalation")

    for key, value in kwargs.items():
        if value is not None:
            arg_name = key.replace("_", "-")
            cmd.extend([f"--{arg_name}", str(value)])

    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def create_receipt(path: str, content: str) -> None:
    """Create a receipt file with the given content."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(content)


def main():
    h = Harness("FLOW-VERIFY-RECEIPT ESCALATION MODE TEST SUITE")

    print("[Test 1] ESCALATE: auto sonnet — all conditions met")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """ATTEMPT: 1
FAILURE_CLASS: implementer-shortfall
IMPLEMENTER_MODEL: haiku
UNITS: 1
TURNS_USED: 45
MAX_TURNS: 120
TURNS_EXHAUSTED: no
INCOMPLETE_STEPS:
  - 1: do the thing
Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            stop_source="receipt",
            escalations=0,
            failures=1,
            implementer_model_state="unset"
        )
        h.test_result(
            "auto escalation: all conditions met",
            exit_code == 0 and "ESCALATE: auto sonnet" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 2] ESCALATE: auto sonnet — fix step variant")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """ATTEMPT: 1
FAILURE_CLASS: implementer-shortfall
IMPLEMENTER_MODEL: haiku
UNITS: 1
TURNS_USED: 50
MAX_TURNS: 120
TURNS_EXHAUSTED: no
INCOMPLETE_STEPS:
  - D1: first directive
Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "fix",
            receipt_path,
            stop_source="receipt",
            escalations=0,
            failures=1,
            implementer_model_state="unset"
        )
        h.test_result(
            "auto escalation: fix step",
            exit_code == 0 and "ESCALATE: auto sonnet" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 3] ESCALATE: manual sonnet — TURNS_EXHAUSTED: yes")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """ATTEMPT: 1
FAILURE_CLASS: implementer-shortfall
IMPLEMENTER_MODEL: haiku
UNITS: 1
TURNS_USED: 120
MAX_TURNS: 120
TURNS_EXHAUSTED: yes
INCOMPLETE_STEPS:
  - 1: incomplete
Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            stop_source="receipt",
            escalations=0,
            failures=1,
            implementer_model_state="unset"
        )
        h.test_result(
            "manual escalation: TURNS_EXHAUSTED: yes",
            exit_code == 0 and "ESCALATE: manual sonnet" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 4] ESCALATE: manual sonnet — TURNS_EXHAUSTED: unknown")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """ATTEMPT: 1
FAILURE_CLASS: implementer-shortfall
IMPLEMENTER_MODEL: haiku
UNITS: 1
TURNS_USED: unknown
MAX_TURNS: 120
TURNS_EXHAUSTED: unknown
INCOMPLETE_STEPS:
  - 1: incomplete
Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            stop_source="receipt",
            escalations=0,
            failures=1,
            implementer_model_state="unset"
        )
        h.test_result(
            "manual escalation: TURNS_EXHAUSTED: unknown",
            exit_code == 0 and "ESCALATE: manual sonnet" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 5] ESCALATE: manual sonnet — TURNS_EXHAUSTED line missing")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """ATTEMPT: 1
FAILURE_CLASS: implementer-shortfall
IMPLEMENTER_MODEL: haiku
UNITS: 1
TURNS_USED: 45
MAX_TURNS: 120
INCOMPLETE_STEPS:
  - 1: incomplete
Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            stop_source="receipt",
            escalations=0,
            failures=1,
            implementer_model_state="unset"
        )
        h.test_result(
            "manual escalation: TURNS_EXHAUSTED missing",
            exit_code == 0 and "ESCALATE: manual sonnet" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 6] ESCALATE: manual sonnet — stop-source validation")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """ATTEMPT: 1
FAILURE_CLASS: implementer-shortfall
IMPLEMENTER_MODEL: haiku
UNITS: 1
TURNS_USED: 45
MAX_TURNS: 120
TURNS_EXHAUSTED: no
INCOMPLETE_STEPS:
  - 1: incomplete
Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            stop_source="validation",
            escalations=0,
            failures=1,
            implementer_model_state="unset"
        )
        h.test_result(
            "manual escalation: stop-source validation",
            exit_code == 0 and "ESCALATE: manual sonnet" in stdout and "stop-source validation" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 7] ESCALATE: manual sonnet — stop-source audit")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """ATTEMPT: 1
FAILURE_CLASS: implementer-shortfall
IMPLEMENTER_MODEL: haiku
UNITS: 1
TURNS_USED: 45
MAX_TURNS: 120
TURNS_EXHAUSTED: no
INCOMPLETE_STEPS:
  - 1: incomplete
Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            stop_source="audit",
            escalations=0,
            failures=1,
            implementer_model_state="unset"
        )
        h.test_result(
            "manual escalation: stop-source audit",
            exit_code == 0 and "ESCALATE: manual sonnet" in stdout and "stop-source audit" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 8] ESCALATE: manual sonnet — multi-unit (UNITS: 3, FAILURE_CLASS: other)")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """ATTEMPT: 1
FAILURE_CLASS: other
IMPLEMENTER_MODEL: haiku
UNITS: 3
TURNS_USED: 45
MAX_TURNS: 120
TURNS_EXHAUSTED: no
Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            stop_source="receipt",
            escalations=0,
            failures=1,
            implementer_model_state="unset"
        )
        h.test_result(
            "manual escalation: multi-unit failure",
            exit_code == 0 and "ESCALATE: manual sonnet" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 9] ESCALATE: none — escalations already at 1")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """ATTEMPT: 2
FAILURE_CLASS: implementer-shortfall
IMPLEMENTER_MODEL: sonnet
UNITS: 1
TURNS_USED: 80
MAX_TURNS: 120
TURNS_EXHAUSTED: no
INCOMPLETE_STEPS:
  - 1: incomplete
Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            stop_source="receipt",
            escalations=1,
            failures=2,
            implementer_model_state="sonnet"
        )
        h.test_result(
            "no escalation: escalations already at 1",
            exit_code == 0 and "ESCALATE: none" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 10] ESCALATE: none — failures already at 2")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """ATTEMPT: 2
FAILURE_CLASS: implementer-shortfall
IMPLEMENTER_MODEL: haiku
UNITS: 1
TURNS_USED: 45
MAX_TURNS: 120
TURNS_EXHAUSTED: no
INCOMPLETE_STEPS:
  - 1: incomplete
Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            stop_source="receipt",
            escalations=0,
            failures=2,
            implementer_model_state="unset"
        )
        h.test_result(
            "no escalation: failures already at 2",
            exit_code == 0 and "ESCALATE: none" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 11] ESCALATE: none — implementer-model already sonnet")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """ATTEMPT: 1
FAILURE_CLASS: implementer-shortfall
IMPLEMENTER_MODEL: sonnet
UNITS: 1
TURNS_USED: 45
MAX_TURNS: 120
TURNS_EXHAUSTED: no
INCOMPLETE_STEPS:
  - 1: incomplete
Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            stop_source="receipt",
            escalations=0,
            failures=1,
            implementer_model_state="sonnet"
        )
        h.test_result(
            "no escalation: model already sonnet",
            exit_code == 0 and "ESCALATE: none" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 12] --escalation for non-implement/fix step prints none")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "verify",
            receipt_path,
            stop_source="receipt",
            escalations=0,
            failures=1,
            implementer_model_state="unset"
        )
        h.test_result(
            "non-implement/fix step: print ESCALATE: none step-verify",
            exit_code == 0 and "ESCALATE: none step-verify" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 13] Exit 2: missing --stop-source")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        # Run without stop_source
        cmd = [
            sys.executable,
            str(VERIFIER),
            "--step", "implement",
            "--receipt", receipt_path,
            "--escalation",
            "--escalations", "0",
            "--failures", "1",
            "--implementer-model-state", "unset",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)

        # Exit 2 alone also fits an argparse rejection (e.g. flag unknown on a
        # reverted tree), so require the verifier's own message.
        h.test_result(
            "exit 2: missing --stop-source",
            result.returncode == 2 and "--escalation requires --stop-source" in result.stderr,
            f"exit={result.returncode}, stderr={result.stderr}"
        )

    print()
    print("[Test 14] Exit 2: unknown --stop-source value")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            stop_source="unknown_value",
            escalations=0,
            failures=1,
            implementer_model_state="unset"
        )
        # argparse choices rejects the value before the verifier's own check runs.
        h.test_result(
            "exit 2: unknown --stop-source",
            exit_code == 2 and "invalid choice: 'unknown_value'" in stderr,
            f"exit={exit_code}, stderr={stderr}"
        )

    print()
    print("[Test 15] Exit 2: unreadable receipt")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/nonexistent.md"

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            stop_source="receipt",
            escalations=0,
            failures=1,
            implementer_model_state="unset"
        )
        # The verifier's own missing-receipt check (after argument parsing) must fire.
        h.test_result(
            "exit 2: unreadable receipt",
            exit_code == 2 and "Receipt file not found" in stderr,
            f"exit={exit_code}, stderr={stderr}"
        )

    print()
    print("[Test 16] --expect-model: model matches, exit 0")
    with tempfile.TemporaryDirectory() as tmp_home:
        repo_path = f"{tmp_home}/repo"
        create_git_repo(repo_path)
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True
        ).stdout.strip()

        # Add one new commit
        Path(f"{repo_path}/file2.txt").write_text("new")
        subprocess.run(["git", "add", "file2.txt"], cwd=repo_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add file2"], cwd=repo_path, capture_output=True)

        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """IMPLEMENTER_MODEL: sonnet
ROUND 1
Decision: OK
OUTSTANDING: none
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            escalation=False,
            expect_model="sonnet",
            repo=repo_path,
            branch="HEAD",
            base=base_sha
        )
        h.test_result(
            "--expect-model: model matches",
            exit_code == 0,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 17] --expect-model: model mismatch, exit 1")
    with tempfile.TemporaryDirectory() as tmp_home:
        repo_path = f"{tmp_home}/repo"
        create_git_repo(repo_path)
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True
        ).stdout.strip()

        # Add one new commit
        Path(f"{repo_path}/file2.txt").write_text("new")
        subprocess.run(["git", "add", "file2.txt"], cwd=repo_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add file2"], cwd=repo_path, capture_output=True)

        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """IMPLEMENTER_MODEL: haiku
ROUND 1
Decision: OK
OUTSTANDING: none
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            escalation=False,
            expect_model="sonnet",
            repo=repo_path,
            branch="HEAD",
            base=base_sha
        )
        h.test_result(
            "--expect-model: model mismatch",
            exit_code == 1 and "implementer model mismatch" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 18] DISCARDED_ADDED_FILES: present file, exit 1")
    with tempfile.TemporaryDirectory() as tmp_home:
        repo_path = f"{tmp_home}/repo"
        create_git_repo(repo_path)
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True
        ).stdout.strip()

        # Add a file that should be discarded
        Path(f"{repo_path}/discarded.txt").write_text("this should not be here")
        subprocess.run(["git", "add", "discarded.txt"], cwd=repo_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add discarded"], cwd=repo_path, capture_output=True)

        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """IMPLEMENTER_MODEL: sonnet
DISCARDED_ADDED_FILES:
  - discarded.txt
ROUND 1
Decision: OK
OUTSTANDING: none
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            escalation=False,
            expect_model="sonnet",
            repo=repo_path,
            branch="HEAD",
            base=base_sha
        )
        h.test_result(
            "DISCARDED_ADDED_FILES: file still present",
            exit_code == 1 and "discarded files still present" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 19] DISCARDED_ADDED_FILES: no file, exit 0")
    with tempfile.TemporaryDirectory() as tmp_home:
        repo_path = f"{tmp_home}/repo"
        create_git_repo(repo_path)
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True
        ).stdout.strip()

        # Add and then remove a file
        Path(f"{repo_path}/discarded.txt").write_text("this should be removed")
        subprocess.run(["git", "add", "discarded.txt"], cwd=repo_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add discarded"], cwd=repo_path, capture_output=True)
        # Remove it now and commit the removal
        Path(f"{repo_path}/discarded.txt").unlink()
        subprocess.run(["git", "add", "-u"], cwd=repo_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "remove discarded"], cwd=repo_path, capture_output=True)

        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """IMPLEMENTER_MODEL: sonnet
DISCARDED_ADDED_FILES:
  - discarded.txt
ROUND 1
Decision: OK
OUTSTANDING: none
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            escalation=False,
            expect_model="sonnet",
            repo=repo_path,
            branch="HEAD",
            base=base_sha
        )
        h.test_result(
            "DISCARDED_ADDED_FILES: file not present",
            exit_code == 0,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 20] DISCARDED_ADDED_FILES: template format (  - path)")
    with tempfile.TemporaryDirectory() as tmp_home:
        repo_path = f"{tmp_home}/repo"
        create_git_repo(repo_path)
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True
        ).stdout.strip()

        receipt_path = f"{tmp_home}/receipt.md"
        # Use verbatim template format with two spaces before dash
        receipt = """IMPLEMENTER_MODEL: sonnet
DISCARDED_ADDED_FILES:
  - discarded.txt
ROUND 1
Decision: OK
OUTSTANDING: none
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            escalation=False,
            expect_model="sonnet",
            repo=repo_path,
            branch="HEAD",
            base=base_sha
        )
        h.test_result(
            "DISCARDED_ADDED_FILES: template format accepted",
            exit_code == 0,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 21] Eligibility condition flip: stop-source != receipt (should fail auto)")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """ATTEMPT: 1
FAILURE_CLASS: implementer-shortfall
IMPLEMENTER_MODEL: haiku
UNITS: 1
TURNS_USED: 45
MAX_TURNS: 120
TURNS_EXHAUSTED: no
INCOMPLETE_STEPS:
  - 1: do the thing
Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            stop_source="validation",
            escalations=0,
            failures=1,
            implementer_model_state="unset"
        )
        h.test_result(
            "eligibility: stop-source != receipt means no auto",
            exit_code == 0 and "ESCALATE: manual" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 22] Eligibility condition flip: failure_class != implementer-shortfall (should fail auto)")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """ATTEMPT: 1
FAILURE_CLASS: other
IMPLEMENTER_MODEL: haiku
UNITS: 1
TURNS_USED: 45
MAX_TURNS: 120
TURNS_EXHAUSTED: no
INCOMPLETE_STEPS:
  - 1: do the thing
Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            stop_source="receipt",
            escalations=0,
            failures=1,
            implementer_model_state="unset"
        )
        h.test_result(
            "eligibility: failure_class != implementer-shortfall means no auto",
            exit_code == 0 and "ESCALATE: manual" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 23] Eligibility condition flip: turns_exhausted != no (should fail auto)")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """ATTEMPT: 1
FAILURE_CLASS: implementer-shortfall
IMPLEMENTER_MODEL: haiku
UNITS: 1
TURNS_USED: 120
MAX_TURNS: 120
TURNS_EXHAUSTED: yes
INCOMPLETE_STEPS:
  - 1: do the thing
Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            stop_source="receipt",
            escalations=0,
            failures=1,
            implementer_model_state="unset"
        )
        h.test_result(
            "eligibility: turns_exhausted != no means no auto",
            exit_code == 0 and "ESCALATE: manual" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 24] Eligibility condition flip: implementer_model != haiku (should fail auto)")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """ATTEMPT: 1
FAILURE_CLASS: implementer-shortfall
IMPLEMENTER_MODEL: sonnet
UNITS: 1
TURNS_USED: 45
MAX_TURNS: 120
TURNS_EXHAUSTED: no
INCOMPLETE_STEPS:
  - 1: do the thing
Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            stop_source="receipt",
            escalations=0,
            failures=1,
            implementer_model_state="unset"
        )
        h.test_result(
            "eligibility: implementer_model != haiku means no auto",
            exit_code == 0 and "ESCALATE: none" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 25] Eligibility condition flip: implementer_model_state != unset (should fail auto)")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """ATTEMPT: 1
FAILURE_CLASS: implementer-shortfall
IMPLEMENTER_MODEL: haiku
UNITS: 1
TURNS_USED: 45
MAX_TURNS: 120
TURNS_EXHAUSTED: no
INCOMPLETE_STEPS:
  - 1: do the thing
Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            stop_source="receipt",
            escalations=0,
            failures=1,
            implementer_model_state="sonnet"
        )
        h.test_result(
            "eligibility: implementer_model_state != unset means no auto",
            exit_code == 0 and "ESCALATE: none" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 26] Eligibility condition flip: escalations != 0 (should fail auto)")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """ATTEMPT: 1
FAILURE_CLASS: implementer-shortfall
IMPLEMENTER_MODEL: haiku
UNITS: 1
TURNS_USED: 45
MAX_TURNS: 120
TURNS_EXHAUSTED: no
INCOMPLETE_STEPS:
  - 1: do the thing
Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            stop_source="receipt",
            escalations=1,
            failures=1,
            implementer_model_state="unset"
        )
        h.test_result(
            "eligibility: escalations != 0 means no auto",
            exit_code == 0 and "ESCALATE: none" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 27] Eligibility condition flip: failures >= 2 (should fail auto)")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """ATTEMPT: 1
FAILURE_CLASS: implementer-shortfall
IMPLEMENTER_MODEL: haiku
UNITS: 1
TURNS_USED: 45
MAX_TURNS: 120
TURNS_EXHAUSTED: no
INCOMPLETE_STEPS:
  - 1: do the thing
Decision: FAILED
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        exit_code, stdout, stderr = run_verifier(
            "implement",
            receipt_path,
            stop_source="receipt",
            escalations=0,
            failures=2,
            implementer_model_state="unset"
        )
        h.test_result(
            "eligibility: failures >= 2 means no auto",
            exit_code == 0 and "ESCALATE: none" in stdout,
            f"exit={exit_code}, stdout={stdout}"
        )

    print()
    print("[Test 28] --expect-model with escalations=1: model must match")
    with tempfile.TemporaryDirectory() as tmp_home:
        repo_path = f"{tmp_home}/repo"
        create_git_repo(repo_path)
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True
        ).stdout.strip()

        # Add one new commit
        Path(f"{repo_path}/file2.txt").write_text("new")
        subprocess.run(["git", "add", "file2.txt"], cwd=repo_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add file2"], cwd=repo_path, capture_output=True)

        receipt_path = f"{tmp_home}/receipt.md"
        receipt = """IMPLEMENTER_MODEL: sonnet
ROUND 1
Decision: OK
OUTSTANDING: none
<!-- step-end -->"""
        create_receipt(receipt_path, receipt)

        # Pass escalations=1 and expect-model
        cmd = [
            sys.executable,
            str(VERIFIER),
            "--step", "implement",
            "--receipt", receipt_path,
            "--expect-model", "sonnet",
            "--escalations", "1",
            "--repo", repo_path,
            "--branch", "HEAD",
            "--base", base_sha,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)

        h.test_result(
            "--expect-model with escalations=1: model matches",
            result.returncode == 0,
            f"exit={result.returncode}, stdout={result.stdout}"
        )

    print()
    h.summarize_and_exit()


if __name__ == "__main__":
    main()
