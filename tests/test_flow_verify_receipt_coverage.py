#!/usr/bin/env python3
"""
Focused coverage-mode tests for scripts/flow-verify-receipt.py --coverage flag.

Tests the plan-coverage table feature (Step 5 and part of Step 6 of #320):
- Coverage mode enumerates plan units (Step N headings for implement)
- Classification: covered (file in diff), declared (in OUTSTANDING:), or uncovered (neither)
- Edge cases: no headings, unreadable plan, unknown declared units
- Fix mode enumerates D<n> from action plan, not the implement plan

Run with: python3 tests/test_flow_verify_receipt_coverage.py
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
    """Create a plan with `steps` step headings (with backtick-quoted files with paths)."""
    content = "# Plan\n\n"
    for i in range(1, steps + 1):
        # Use paths with "/" to be recognized as repo paths
        content += f"### Step {i}\n\nContent for step {i}. File: `src/step{i}.py`\n\n"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(content)


def create_receipt(path: str, outstanding_lines: list = None) -> None:
    """Create a receipt with optional OUTSTANDING lines (no blank lines)."""
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
    h = Harness("COVERAGE MODE TESTS")

    print("[Test 1] Basic coverage mode: Step 1 covered, Step 2 uncovered")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        # src/step1.py is in the diff (first file gets modified), src/step2.py is not
        base_sha, branch_sha = create_git_repo_with_files(repo_path, ["src/step1.py", "src/step2.py"])

        plan_path = f"{tmpdir}/plan.md"
        create_plan(plan_path, 2)

        receipt_path = f"{tmpdir}/receipt.md"
        # Don't declare Step 1 so it shows up as covered instead of declared
        create_receipt(receipt_path, ["Step 3 (out of scope) [self-report]"])

        exit_code, stdout, stderr = run_verifier_coverage(
            receipt_path, plan_path, repo_path, "HEAD", base_sha
        )

        # Check for exact covered and uncovered rows with pipe delimiter
        passed = (
            exit_code == 0
            and "Step 1 | covered | " in stdout
            and "Step 2 | uncovered | " in stdout
        )
        h.test_result("Basic coverage: Step 1 covered, Step 2 uncovered", passed, f"exit_code={exit_code}, stdout:\n{stdout}")

    print()
    print("[Test 2] Coverage mode: declared items from OUTSTANDING:")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, branch_sha = create_git_repo_with_files(repo_path, ["step1.txt"])

        plan_path = f"{tmpdir}/plan.md"
        create_plan(plan_path, 3)

        receipt_path = f"{tmpdir}/receipt.md"
        create_receipt(receipt_path, [
            "Step 2 (deferred) [self-report]",
            "Step 3 (out of scope) [both]"
        ])

        exit_code, stdout, stderr = run_verifier_coverage(
            receipt_path, plan_path, repo_path, "HEAD", base_sha
        )

        # Check for exact declared rows with pipe delimiter
        passed = (
            exit_code == 0
            and "Step 2 | declared | " in stdout
            and "Step 3 | declared | " in stdout
        )
        h.test_result("Declared items show as declared", passed, f"exit_code={exit_code}, stdout:\n{stdout}")

    print()
    print("[Test 3] Plan with no step headings -> UNITS: 0")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, branch_sha = create_git_repo_with_files(repo_path, ["file.txt"])

        plan_path = f"{tmpdir}/plan.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan\n\nNo steps here, just content.")

        receipt_path = f"{tmpdir}/receipt.md"
        create_receipt(receipt_path)

        exit_code, stdout, stderr = run_verifier_coverage(
            receipt_path, plan_path, repo_path, "HEAD", base_sha
        )

        # Verify UNITS header starts with "UNITS: 0" and message is present
        lines = stdout.split('\n') if stdout else []
        units_line = next((ln for ln in lines if ln.startswith("UNITS:")), None)
        has_units_header = units_line and units_line.startswith("UNITS: 0")
        passed = (
            exit_code == 0
            and has_units_header
            and "no step headings" in stdout
        )
        h.test_result("Plan with no step headings exits 0 with UNITS: 0", passed, f"exit_code={exit_code}, units_line={units_line}, stdout:\n{stdout}")

    print()
    print("[Test 4] Unreadable plan -> exit 2")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, branch_sha = create_git_repo_with_files(repo_path, ["file.txt"])

        plan_path = f"{tmpdir}/nonexistent.md"  # File doesn't exist

        receipt_path = f"{tmpdir}/receipt.md"
        create_receipt(receipt_path)

        exit_code, stdout, stderr = run_verifier_coverage(
            receipt_path, plan_path, repo_path, "HEAD", base_sha
        )

        passed = exit_code == 2
        h.test_result("Unreadable plan exits 2", passed, f"exit_code={exit_code}, stderr={stderr}")

    print()
    print("[Test 5] Unknown declared item (not in plan)")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, branch_sha = create_git_repo_with_files(repo_path, ["step1.txt"])

        plan_path = f"{tmpdir}/plan.md"
        create_plan(plan_path, 2)

        receipt_path = f"{tmpdir}/receipt.md"
        # Declare a step that doesn't exist in the plan
        create_receipt(receipt_path, ["Step 99 (phantom) [self-report]"])

        exit_code, stdout, stderr = run_verifier_coverage(
            receipt_path, plan_path, repo_path, "HEAD", base_sha
        )

        # Should still succeed and list the unknown declared item
        passed = (
            exit_code == 0
            and "unknown-declared: Step 99" in stdout
        )
        h.test_result("Unknown declared item listed as unknown-declared", passed, f"exit_code={exit_code}, stdout:\n{stdout}")

    print()
    print("[Test 6] Fix mode: enumerate D<n> from action plan, not implement plan")
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = f"{tmpdir}/repo"
        base_sha, branch_sha = create_git_repo_with_files(repo_path, ["d1.txt"])

        # Create an action plan with proper "## Doing it:" table format
        action_plan_path = f"{tmpdir}/action-plan.md"
        action_plan = """# Action Plan

## Doing it:

| Item | Work |
|------|------|
| D1 | Fix the linting issue |
| D2 | Add error handling |

"""
        Path(action_plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(action_plan_path).write_text(action_plan)

        # Create implement plan (should be ignored in fix mode)
        plan_path = f"{tmpdir}/plan.md"
        create_plan(plan_path, 3)

        receipt_path = f"{tmpdir}/receipt.md"
        create_receipt(receipt_path, ["D2 (pending) [self-report]"])

        exit_code, stdout, stderr = run_verifier_coverage(
            receipt_path, action_plan_path, repo_path, "HEAD", base_sha, step="fix"
        )

        # In fix mode, should enumerate D<n> from action plan table (not Step <n>)
        # Files aren't extracted from action plan table rows, so D1 is uncovered and D2 is declared
        passed = (
            exit_code == 0
            and "D1 | uncovered | " in stdout  # D1 is enumerated as uncovered
            and "D2 | declared | " in stdout  # D2 is declared in OUTSTANDING
            and "Step 1" not in stdout  # Should not mention implement steps
        )
        h.test_result("Fix mode enumerates D<n> and ignores implement plan", passed, f"exit_code={exit_code}, stdout:\n{stdout}")

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
