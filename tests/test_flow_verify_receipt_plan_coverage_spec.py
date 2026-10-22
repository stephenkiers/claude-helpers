#!/usr/bin/env python3
"""
Spec-blind tests for plan-coverage audit behaviors from #320 action plan.

Testable behaviors from the plan:
- Unknown state (cluster item 1, option A): non-blocking unknown for no evidence, git failure, fix row without files
- Anchored unit-id lookup (F7): unit 1 must not match unit 12
- --base/--branch validation (F13): reject arguments starting with -
- Table separator alignment colons (F14): accept :?-+:? per cell
- OUTSTANDING validation (F2): blank lines, none mixed with items rejected
- File path normalization (F7, F8): strip ./ prefix and :line suffix

Run with: python3 tests/test_flow_verify_receipt_plan_coverage_spec.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

VERIFIER = REPO_ROOT / "scripts" / "flow-verify-receipt.py"


def run_verifier_coverage(receipt_path: str, plan_path: str, repo: str, branch: str, base: str, step: str = "implement") -> tuple:
    """Run verifier in coverage mode and return (exit_code, stdout, stderr)."""
    cmd = [
        sys.executable,
        str(VERIFIER),
        "--step", step,
        "--receipt", receipt_path,
        "--coverage",
        "--plan", plan_path,
        "--repo", repo,
        "--branch", branch,
        "--base", base,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def run_verifier_coverage_with_opts(receipt_path: str, plan_path: str, repo: str, branch: str, base: str, step: str = "implement", **opts) -> tuple:
    """Run verifier with additional options."""
    cmd = [
        sys.executable,
        str(VERIFIER),
        "--step", step,
        "--receipt", receipt_path,
        "--coverage",
        "--plan", plan_path,
        "--repo", repo,
        "--branch", branch,
        "--base", base,
    ]
    for key, val in opts.items():
        if val is not None:
            arg_name = key.replace("_", "-")
            cmd.extend([f"--{arg_name}", str(val)])
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def create_git_repo_with_files(repo_path: str, files: list) -> tuple:
    """Create a git repo with specified files in base, return (base_sha, branch_sha)."""
    Path(repo_path).mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_path, capture_output=True, check=True)

    # Base commit: create all files
    for f in files:
        Path(repo_path, f).parent.mkdir(parents=True, exist_ok=True)
        Path(repo_path, f).write_text(f"content of {f}")

    subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo_path, capture_output=True, check=True)
    base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True, check=True).stdout.strip()

    # Branch: modify subset of files to show diff
    if files:
        Path(repo_path, files[0]).write_text(f"modified: {files[0]}")
        subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "branch"], cwd=repo_path, capture_output=True, check=True)

    branch_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True, check=True).stdout.strip()
    return base_sha, branch_sha


def create_plan(path: str, steps: int) -> None:
    """Create a plan with `steps` step headings."""
    content = "# Plan\n\n"
    for i in range(1, steps + 1):
        content += f"### Step {i}\n\nContent for step {i}.\n\n"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(content)


def create_receipt(path: str, outstanding_lines: list = None) -> None:
    """Create a receipt with optional OUTSTANDING lines."""
    content = "Decision: OK\n"
    if outstanding_lines:
        for line in outstanding_lines:
            content += f"OUTSTANDING: {line}\n"
    else:
        content += "OUTSTANDING: none\n"
    content += "<!-- step-end -->"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(content)


def main():
    h = Harness("PLAN COVERAGE SPEC-BLIND TESTS (#320)")

    print("[Test 1] Unknown state: no extractable evidence in fix mode")
    print("  Behavior: fix row without file paths => unknown (non-blocking)")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, _ = create_git_repo_with_files(repo_path, ["file.txt"])

        # Action plan with D1 having no file paths
        action_plan_path = f"{tmpdir}/action-plan.md"
        action_plan = """# Action Plan

## Doing it:

| Item | Work |
|------|------|
| D1 | Fix something with no files |
| D2 | Fix something else |

"""
        Path(action_plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(action_plan_path).write_text(action_plan)

        receipt_path = f"{tmpdir}/receipt.md"
        create_receipt(receipt_path)

        exit_code, stdout, stderr = run_verifier_coverage(
            receipt_path, action_plan_path, repo_path, "HEAD", base_sha, step="fix"
        )

        # D1 should be unknown (no files), not uncovered
        passed = (
            exit_code == 0
            and "D1 | unknown | " in stdout
            and "D2 | unknown | " in stdout
        )
        h.test_result("D1, D2 marked as unknown (no extractable files)", passed, f"exit_code={exit_code}, stdout:\n{stdout}")

    print()
    print("[Test 2] Unknown state: git failure returns unknown, not all-uncovered")
    print("  Behavior: git failure => unknown")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, _ = create_git_repo_with_files(repo_path, ["step1.txt"])

        plan_path = f"{tmpdir}/plan.md"
        create_plan(plan_path, 2)

        receipt_path = f"{tmpdir}/receipt.md"
        create_receipt(receipt_path)

        # Use a non-existent repo to trigger git failure
        bad_repo = f"{tmpdir}/nonexistent_repo"
        exit_code, stdout, stderr = run_verifier_coverage(
            receipt_path, plan_path, bad_repo, "HEAD", base_sha
        )

        # git failure should exit 0 with unknown state (non-blocking), not all-uncovered
        # [WEAK_ASSERTION strengthened] Verify both units show unknown, not just substring match
        passed = exit_code == 0 and all(
            f"Step {i} | unknown | " in stdout for i in range(1, 3)
        )
        h.test_result("Git failure on bad repo returns unknown state", passed, f"exit_code={exit_code}, stdout:\n{stdout}")

    print()
    print("[Test 3] Anchored unit-id lookup: Step 1 does not match Step 12")
    print("  Behavior: unit-id lookup is anchored (\\b(?!\\d))")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        # Create base commit with both files
        Path(repo_path).mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init"], cwd=repo_path, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_path, capture_output=True, check=True)

        Path(repo_path, "step1.txt").write_text("content of step1.txt")
        Path(repo_path, "step12.txt").write_text("content of step12.txt")
        subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=repo_path, capture_output=True, check=True)
        base_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True, check=True).stdout.strip()

        # Modify both files to show in diff
        Path(repo_path, "step1.txt").write_text("modified: step1.txt")
        Path(repo_path, "step12.txt").write_text("modified: step12.txt")
        subprocess.run(["git", "add", "."], cwd=repo_path, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "branch"], cwd=repo_path, capture_output=True, check=True)

        plan_path = f"{tmpdir}/plan.md"
        content = "# Plan\n\n### Step 1\n\nModify `step1.txt`.\n\n### Step 12\n\nModify `step12.txt`.\n\n"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text(content)

        receipt_path = f"{tmpdir}/receipt.md"
        content = """Decision: OK
OUTSTANDING: Step 1 (deferred) [self-report]
<!-- step-end -->"""
        Path(receipt_path).parent.mkdir(parents=True, exist_ok=True)
        Path(receipt_path).write_text(content)

        exit_code, stdout, stderr = run_verifier_coverage(
            receipt_path, plan_path, repo_path, "HEAD", base_sha
        )

        # Step 1 is declared, Step 12 should be covered (in diff, and anchored match ensures they don't mix)
        passed = (
            exit_code == 0
            and "Step 1 | declared | " in stdout
            and "Step 12 | covered | " in stdout
        )
        h.test_result("Step 1 declared, Step 12 covered (not matched by Step 1)", passed, f"exit_code={exit_code}, stdout:\n{stdout}")

    print()
    print("[Test 4] File path normalization: strip ./ prefix")
    print("  Behavior: ./file.py normalized to file.py")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, _ = create_git_repo_with_files(repo_path, ["src/file.py"])

        plan_path = f"{tmpdir}/plan.md"
        content = "# Plan\n\n### Step 1\n\nFile: `./src/file.py`\n\n"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text(content)

        receipt_path = f"{tmpdir}/receipt.md"
        create_receipt(receipt_path)

        exit_code, stdout, stderr = run_verifier_coverage(
            receipt_path, plan_path, repo_path, "HEAD", base_sha
        )

        # Step 1 should be covered (./src/file.py normalized and found in diff)
        passed = (
            exit_code == 0
            and "Step 1 | covered | " in stdout
        )
        h.test_result("Path ./src/file.py normalized and matched in diff", passed, f"exit_code={exit_code}, stdout:\n{stdout}")

    print()
    print("[Test 5] File path normalization: strip :line suffix")
    print("  Behavior: file.py:42 normalized to file.py")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, _ = create_git_repo_with_files(repo_path, ["main.py"])

        plan_path = f"{tmpdir}/plan.md"
        content = "# Plan\n\n### Step 1\n\nFile: `main.py:42`\n\n"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text(content)

        receipt_path = f"{tmpdir}/receipt.md"
        create_receipt(receipt_path)

        exit_code, stdout, stderr = run_verifier_coverage(
            receipt_path, plan_path, repo_path, "HEAD", base_sha
        )

        # Step 1 should be covered (main.py:42 normalized and found in diff)
        passed = (
            exit_code == 0
            and "Step 1 | covered | " in stdout
        )
        h.test_result("Path main.py:42 normalized and matched in diff", passed, f"exit_code={exit_code}, stdout:\n{stdout}")

    print()
    print("[Test 6] Table separator alignment colons: :--|--: pattern")
    print("  Behavior: separator rows allow :?-+:? per cell")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, _ = create_git_repo_with_files(repo_path, ["d1.txt"])

        action_plan_path = f"{tmpdir}/action-plan.md"
        action_plan = """# Action Plan

## Doing it:

| Item | Work |
|:---:|:----:|
| D1 | First |
| D2 | Second |

"""
        Path(action_plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(action_plan_path).write_text(action_plan)

        receipt_path = f"{tmpdir}/receipt.md"
        create_receipt(receipt_path)

        exit_code, stdout, stderr = run_verifier_coverage(
            receipt_path, action_plan_path, repo_path, "HEAD", base_sha, step="fix"
        )

        # Should correctly parse table with centered alignment colons
        passed = (
            exit_code == 0
            and "D1 | unknown | " in stdout
            and "D2 | unknown | " in stdout
        )
        h.test_result("Table with centered alignment colons parsed correctly", passed, f"exit_code={exit_code}, stdout:\n{stdout}")

    print()
    print("[Test 7] OUTSTANDING validation: blank lines rejected")
    print("  Behavior: blank lines in OUTSTANDING block cause validation error")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, _ = create_git_repo_with_files(repo_path, ["file.txt"])

        plan_path = f"{tmpdir}/plan.md"
        create_plan(plan_path, 2)

        receipt_path = f"{tmpdir}/receipt.md"
        content = """Decision: OK
OUTSTANDING: Step 1 (deferred) [self-report]

OUTSTANDING: Step 2 (deferred) [self-report]
<!-- step-end -->"""
        Path(receipt_path).parent.mkdir(parents=True, exist_ok=True)
        Path(receipt_path).write_text(content)

        exit_code, stdout, stderr = run_verifier_coverage(
            receipt_path, plan_path, repo_path, "HEAD", base_sha
        )

        # Blank line in OUTSTANDING block should fail validation (exit 2)
        # [WEAK_ASSERTION strengthened] Check specific error exit code, not just != 0
        passed = exit_code == 2 and "blank line" in stderr.lower()
        h.test_result("Blank line in OUTSTANDING block causes validation error", passed, f"exit_code={exit_code}, stderr={stderr[:100]}")

    print()
    print("[Test 8] OUTSTANDING validation: 'none' mixed with items rejected")
    print("  Behavior: 'none' and actual items in same OUTSTANDING block is invalid")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, _ = create_git_repo_with_files(repo_path, ["file.txt"])

        plan_path = f"{tmpdir}/plan.md"
        create_plan(plan_path, 1)

        receipt_path = f"{tmpdir}/receipt.md"
        content = """Decision: OK
OUTSTANDING: none
OUTSTANDING: Step 1 (deferred) [self-report]
<!-- step-end -->"""
        Path(receipt_path).parent.mkdir(parents=True, exist_ok=True)
        Path(receipt_path).write_text(content)

        exit_code, stdout, stderr = run_verifier_coverage(
            receipt_path, plan_path, repo_path, "HEAD", base_sha
        )

        # Mixing 'none' with items should fail validation (exit 2)
        # [WEAK_ASSERTION strengthened] Check specific error exit code and error message
        passed = exit_code == 2 and "mixes" in stderr.lower()
        h.test_result("OUTSTANDING: none mixed with items is invalid", passed, f"exit_code={exit_code}, stderr={stderr[:100]}")

    print()
    print("[Test 9] --base argument starting with - is rejected")
    print("  Behavior: --base -something is rejected (F13)")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, _ = create_git_repo_with_files(repo_path, ["file.txt"])

        plan_path = f"{tmpdir}/plan.md"
        create_plan(plan_path, 1)

        receipt_path = f"{tmpdir}/receipt.md"
        create_receipt(receipt_path)

        # Try to run with --base starting with dash
        cmd = [
            sys.executable,
            str(VERIFIER),
            "--step", "implement",
            "--receipt", receipt_path,
            "--coverage",
            "--plan", plan_path,
            "--repo", repo_path,
            "--branch", "HEAD",
            "--base", "-invalid-base",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)

        # Should fail or reject the argument (exit 2 for bad args)
        # [WEAK_ASSERTION strengthened] Check specific error exit code
        passed = result.returncode == 2
        h.test_result("--base starting with - is rejected", passed, f"exit_code={result.returncode}, stderr={result.stderr[:100]}")

    print()
    print("[Test 10] --branch argument starting with - is rejected")
    print("  Behavior: --branch -something is rejected (F13)")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, _ = create_git_repo_with_files(repo_path, ["file.txt"])

        plan_path = f"{tmpdir}/plan.md"
        create_plan(plan_path, 1)

        receipt_path = f"{tmpdir}/receipt.md"
        create_receipt(receipt_path)

        # Try to run with --branch starting with dash
        cmd = [
            sys.executable,
            str(VERIFIER),
            "--step", "implement",
            "--receipt", receipt_path,
            "--coverage",
            "--plan", plan_path,
            "--repo", repo_path,
            "--branch", "-invalid-branch",
            "--base", base_sha,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)

        # Should fail or reject the argument (exit 2 for bad args)
        # [WEAK_ASSERTION strengthened] Check specific error exit code
        passed = result.returncode == 2
        h.test_result("--branch starting with - is rejected", passed, f"exit_code={result.returncode}, stderr={result.stderr[:100]}")

    print()
    print("[Test 11] Unknown state is non-blocking (does not cause FIDELITY verdict)")
    print("  Behavior: unknown classification never alone causes 'reduced plan coverage'")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, _ = create_git_repo_with_files(repo_path, ["file.txt"])

        # Action plan with items having no files
        action_plan_path = f"{tmpdir}/action-plan.md"
        action_plan = """# Action Plan

## Doing it:

| Item | Work |
|------|------|
| D1 | No files here |

"""
        Path(action_plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(action_plan_path).write_text(action_plan)

        receipt_path = f"{tmpdir}/receipt.md"
        create_receipt(receipt_path)

        exit_code, stdout, stderr = run_verifier_coverage(
            receipt_path, action_plan_path, repo_path, "HEAD", base_sha, step="fix"
        )

        # Should exit 0 (non-blocking), not have FIDELITY error
        passed = (
            exit_code == 0
            and "D1 | unknown | " in stdout
            and "reduced plan coverage" not in stdout.lower()
        )
        h.test_result("Unknown state is non-blocking, no FIDELITY verdict", passed, f"exit_code={exit_code}, stdout:\n{stdout}")

    print()
    print("[Test 12] Mixed coverage states: covered, declared, unknown")
    print("  Behavior: table shows mixed states correctly")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, _ = create_git_repo_with_files(repo_path, ["covered.py"])

        plan_path = f"{tmpdir}/plan.md"
        content = "# Plan\n\n### Step 1\n\nHas file: `covered.py`\n\n### Step 2\n\nNo file.\n\n### Step 3\n\nOther file.\n\n"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text(content)

        receipt_path = f"{tmpdir}/receipt.md"
        content = """Decision: OK
OUTSTANDING: Step 3 (deferred) [self-report]
<!-- step-end -->"""
        Path(receipt_path).parent.mkdir(parents=True, exist_ok=True)
        Path(receipt_path).write_text(content)

        exit_code, stdout, stderr = run_verifier_coverage(
            receipt_path, plan_path, repo_path, "HEAD", base_sha
        )

        # Step 1: covered, Step 2: uncovered, Step 3: declared
        passed = (
            exit_code == 0
            and "Step 1 | covered | " in stdout
            and "Step 2 | uncovered | " in stdout
            and "Step 3 | declared | " in stdout
        )
        h.test_result("Mixed coverage states shown correctly", passed, f"exit_code={exit_code}, stdout:\n{stdout}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
