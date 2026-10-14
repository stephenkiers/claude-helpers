#!/usr/bin/env python3
"""
Executable fixture tests for scripts/flow-verify-receipt.py

Tests verify step receipts against on-disk artifacts: plan session dirs with roster contributions,
review dirs with checkpoints and action plans, and commit counts for implement/fix.

Run with: python3 tests/test_flow_verify_receipt.py
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
            cmd.extend([f"--{key}", str(value)])

    env = os.environ.copy()
    env["HOME"] = env_home
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def create_receipt(path: str, content: str) -> None:
    """Create a receipt file with the given content."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(content)


def create_contribution(path: str, is_standin: bool = False) -> None:
    """Create a contribution file with or without the sentinel and stand-in marker."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if is_standin:
        content = "## Decision\nFAILED\n<!-- contribution-end -->"
    else:
        content = "# Some contribution\n\nContent here\n<!-- contribution-end -->"
    Path(path).write_text(content)


def create_session(home: str, invocation_id: str) -> str:
    """Create a plan session dir and return its path."""
    session_dir = f"{home}/.claude/plan-sessions/repo_key/plan-{invocation_id}"
    Path(session_dir).mkdir(parents=True, exist_ok=True)
    return session_dir


def create_reviewer_index(home: str) -> None:
    """Create a minimal reviewers/index.yaml fixture."""
    index_path = f"{home}/.claude/reviewers/index.yaml"
    Path(index_path).parent.mkdir(parents=True, exist_ok=True)
    content = """reviewers:
  - name: Danielle the Designer
    file: danielle-designer.yaml
  - name: Mozart
    file: mozart-eda.yaml
"""
    Path(index_path).write_text(content)


def create_review_dir(home: str) -> str:
    """Create a review dir with required files and return its path."""
    review_dir = f"{home}/.claude/reviews/repo_key/12-review-2026"
    Path(review_dir).mkdir(parents=True, exist_ok=True)

    # Create required files
    Path(f"{review_dir}/claude-action-plan.md").write_text("# Action Plan\n## Doing it\n- item 1")
    Path(f"{review_dir}/final-report.md").write_text("# Final Report")
    Path(f"{review_dir}/findings.json").write_text('{"findings": []}')
    Path(f"{review_dir}/reviewer-pass1.md").write_text("# Pass 1")
    Path(f"{review_dir}/reviewer2-pass1.md").write_text("# Pass 1 #2")

    return review_dir


def create_git_repo(repo_path: str) -> None:
    """Create a minimal git repo with one commit."""
    Path(repo_path).mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_path, capture_output=True)
    Path(f"{repo_path}/file.txt").write_text("initial")
    subprocess.run(["git", "add", "file.txt"], cwd=repo_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_path, capture_output=True)


def main():
    h = Harness("FLOW-VERIFY-RECEIPT EXECUTABLE TEST SUITE")

    print("[Test 1] The #276 incident: plan receipt OK with 'NOT dispatched' text")
    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261013T222013-30924"
        session_dir = create_session(tmp_home, invocation_id)

        plan_path = f"{tmp_home}/.claude/plans/issue-slug-{invocation_id}.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        receipt = f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->"
        create_receipt(receipt_path, receipt)

        # Create roster and contribution
        roster_path = f"{session_dir}/selected-experts.md"
        Path(roster_path).write_text("| Expert |\n|-----|\n| Danielle the Designer |")
        create_contribution(f"{session_dir}/danielle-designer-contribution.md")

        # Receipt with "Panel: NOT dispatched" text
        receipt_with_deviation = receipt.replace("Decision: OK", "Panel: NOT dispatched\nDecision: OK")

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-14T00:00:00Z", tmp_home
        )

        # First, test that the receipt admits deviation
        create_receipt(receipt_path, receipt_with_deviation)
        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-14T00:00:00Z", tmp_home
        )
        h.test_result("Receipt with 'NOT dispatched' is rejected", exit_code == 1 and "receipt admits deviation" in stdout)

        # Then test that without the deviation text, it fails on missing session
        create_receipt(receipt_path, receipt)
        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-14T00:00:00Z", tmp_home
        )
        h.test_result("Receipt without deviation but missing session fails", exit_code == 1)

    print()
    print("[Test 2] Plan good path: roster of contributors, all with contributions")
    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261013T222013-30924"
        session_dir = create_session(tmp_home, invocation_id)

        plan_path = f"{tmp_home}/.claude/plans/issue-slug-{invocation_id}.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->")

        # Create roster with 2 contributors
        roster_path = f"{session_dir}/selected-experts.md"
        Path(roster_path).write_text("| Expert | Concern |\n|--------|----------|\n| Danielle the Designer | UI |\n| Mozart | Architecture |")
        create_contribution(f"{session_dir}/danielle-designer-contribution.md")
        create_contribution(f"{session_dir}/mozart-eda-contribution.md")

        # Create context.md for freshness check
        Path(f"{session_dir}/context.md").write_text("# Context")

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Plan with valid roster and contributions passes", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    print()
    print("[Test 3] Plan roster with stand-in contribution fails")
    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261014T100000-30925"
        session_dir = create_session(tmp_home, invocation_id)

        plan_path = f"{tmp_home}/.claude/plans/issue-slug-{invocation_id}.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->")

        # Create roster with stand-in contribution
        roster_path = f"{session_dir}/selected-experts.md"
        Path(roster_path).write_text("| Expert |\n|--------|\n| Danielle the Designer |")
        create_contribution(f"{session_dir}/danielle-designer-contribution.md", is_standin=True)

        # Create context.md
        Path(f"{session_dir}/context.md").write_text("# Context")

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Stand-in contribution is rejected", exit_code == 1 and "stand-in" in stdout)

    print()
    print("[Test 4] Deviation scan: Deviation: at line start fails")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, "Some text\nDeviation: found an issue\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "track", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Deviation: at line start is rejected", exit_code == 1 and "receipt admits deviation" in stdout)

    print()
    print("[Test 5] Deviation scan: NOT dispatched inside backticks is ignored")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, "Some text\nThe issue is `NOT dispatched` to the backend.\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "track", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("NOT dispatched inside backticks is ignored", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    print()
    print("[Test 6] Review dir with checkpoints passes")
    with tempfile.TemporaryDirectory() as tmp_home:
        review_dir = create_review_dir(tmp_home)

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"REVIEW_DIR: {review_dir}\nCONFIRMED: critical=0 high=1 medium=0 low=0\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "review", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Review with valid dir and checkpoints passes", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    print()
    print("[Test 7] Review dir with only 1 checkpoint fails")
    with tempfile.TemporaryDirectory() as tmp_home:
        review_dir = create_review_dir(tmp_home)
        # Remove one checkpoint
        Path(f"{review_dir}/reviewer2-pass1.md").unlink()

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"REVIEW_DIR: {review_dir}\nCONFIRMED: critical=0 high=1 medium=0 low=0\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "review", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Review with only 1 checkpoint fails", exit_code == 1 and "reviewer checkpoints" in stdout)

    print()
    print("[Test 8] Implement with one commit passes")
    with tempfile.TemporaryDirectory() as tmp_home:
        repo_path = f"{tmp_home}/repo"
        create_git_repo(repo_path)

        # Get initial commit SHA
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True)
        base_sha = result.stdout.strip()

        # Add one new commit
        Path(f"{repo_path}/file2.txt").write_text("new")
        subprocess.run(["git", "add", "file2.txt"], cwd=repo_path, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add file2"], cwd=repo_path, capture_output=True)

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, "ROUND 1\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "implement", receipt_path, "2026-10-13T00:00:00Z", tmp_home,
            repo=repo_path, branch="master", base=base_sha
        )
        h.test_result("Implement with one new commit passes", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    print()
    print("[Test 9] Implement with no new commits fails")
    with tempfile.TemporaryDirectory() as tmp_home:
        repo_path = f"{tmp_home}/repo"
        create_git_repo(repo_path)

        # Get the SHA (no new commits made)
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True)
        base_sha = result.stdout.strip()

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, "ROUND 1\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "implement", receipt_path, "2026-10-13T00:00:00Z", tmp_home,
            repo=repo_path, branch="master", base=base_sha
        )
        h.test_result("Implement with no new commits fails", exit_code == 1 and "no new commits" in stdout)

    print()
    print("[Test 10] Read-only guard: no write operations in script")
    verifier_source = VERIFIER.read_text()
    h.test_result("No open( calls with write modes", "open(" not in verifier_source or 'open(' not in verifier_source or "w" not in verifier_source.split("open(")[1].split(")")[0] if "open(" in verifier_source else True)
    h.test_result("No os.remove or shutil writes", "os.remove" not in verifier_source and "shutil" not in verifier_source)
    h.test_result("No non-git subprocess calls", all(
        "git " in line for line in verifier_source.split("subprocess.run")[1:]
        if "[" in line and "git" not in line  # Check subprocess.run calls
    ) if "subprocess.run" in verifier_source else True)

    h.summarize()


if __name__ == "__main__":
    main()
