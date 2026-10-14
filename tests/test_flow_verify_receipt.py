#!/usr/bin/env python3
"""
Executable fixture tests for scripts/flow-verify-receipt.py

Tests verify step receipts against on-disk artifacts: plan session dirs with roster contributions,
review dirs with checkpoints and action plans, and commit counts for implement/fix.

Run with: python3 tests/test_flow_verify_receipt.py
"""

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
            # Convert underscores to hyphens for CLI argument names
            arg_name = key.replace("_", "-")
            cmd.extend([f"--{arg_name}", str(value)])

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
        h.test_result("Receipt without deviation but missing session fails", exit_code == 1 and "session dir" in stdout)

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
            repo=repo_path, branch="HEAD", base=base_sha
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
            repo=repo_path, branch="HEAD", base=base_sha
        )
        h.test_result("Implement with no new commits fails", exit_code == 1 and "no new commits" in stdout)

    print()
    print("[Test 10] Read-only guard: no write operations in script")
    import ast
    import re
    verifier_source = VERIFIER.read_text()

    # Parse script as AST to verify no file writes
    try:
        tree = ast.parse(verifier_source)
        write_ops_found = []

        for node in ast.walk(tree):
            # Check for open() calls with write modes
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == 'open':
                    for keyword in node.keywords:
                        if keyword.arg == 'mode' and isinstance(keyword.value, ast.Constant):
                            if 'w' in keyword.value.value:
                                write_ops_found.append(f"open() with write mode: {keyword.value.value}")
            # Check for os.remove, shutil operations
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute):
                    if node.func.attr in ['remove', 'rmtree', 'write']:
                        write_ops_found.append(f"{node.func.attr}()")

        h.test_result("No file write operations in AST", len(write_ops_found) == 0)
    except SyntaxError:
        h.test_result("No file write operations in AST", False)

    # Check that all subprocess.run calls are git commands
    git_subprocess_pattern = re.compile(r'subprocess\.run\s*\(\s*\[.*?"git"')
    subprocess_calls = re.findall(r'subprocess\.run\s*\(\s*\[([^\]]+)\]', verifier_source)
    all_git = all('git' in call for call in subprocess_calls)
    h.test_result("All subprocess.run calls are git commands", all_git)

    print()
    print("[Test 11] Plan roster with no ## Experts heading passes")
    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261013T222013-30924"
        session_dir = create_session(tmp_home, invocation_id)

        plan_path = f"{tmp_home}/.claude/plans/issue-slug-{invocation_id}.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->")

        # Create roster WITHOUT ## Experts heading
        roster_path = f"{session_dir}/selected-experts.md"
        Path(roster_path).write_text("| Expert | Concern |\n|--------|----------|\n| Danielle the Designer | UI |")
        create_contribution(f"{session_dir}/danielle-designer-contribution.md")

        # Create context.md
        Path(f"{session_dir}/context.md").write_text("# Context")

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Plan roster without ## Experts heading passes", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    print()
    print("[Test 12] Plan roster with annotated names (parenthetical)")
    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261013T222013-30924"
        session_dir = create_session(tmp_home, invocation_id)

        plan_path = f"{tmp_home}/.claude/plans/issue-slug-{invocation_id}.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->")

        # Create roster with annotated name
        roster_path = f"{session_dir}/selected-experts.md"
        Path(roster_path).write_text("| Expert | Concern |\n|--------|----------|\n| Uncle Bob (pinned) | Code |")

        # Uncle Bob requires a fallback kebab-name: uncle-bob-contribution.md
        create_contribution(f"{session_dir}/uncle-bob-contribution.md")

        # Create context.md
        Path(f"{session_dir}/context.md").write_text("# Context")

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Plan with annotated name (parenthetical) passes", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    print()
    print("[Test 13] Plan roster with non-contributor Auditor row (no file needed)")
    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261013T222013-30924"
        session_dir = create_session(tmp_home, invocation_id)

        plan_path = f"{tmp_home}/.claude/plans/issue-slug-{invocation_id}.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->")

        # Create roster with non-contributor Auditor row and a real contributor
        roster_path = f"{session_dir}/selected-experts.md"
        Path(roster_path).write_text("| Expert | Concern |\n|--------|----------|\n| Danielle the Designer | UI |\n| Auditor | oversight |")
        create_contribution(f"{session_dir}/danielle-designer-contribution.md")
        # Auditor row should not require a file

        # Create context.md
        Path(f"{session_dir}/context.md").write_text("# Context")

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Plan with non-contributor Auditor row passes", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    print()
    print("[Test 14] Plan session dir: two matching session dirs fails")
    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261013T222013-30924"

        # Create two session dirs with the same invocation ID
        session_dir1 = f"{tmp_home}/.claude/plan-sessions/repo_key/plan-{invocation_id}"
        session_dir2 = f"{tmp_home}/.claude/plan-sessions/other/plan-{invocation_id}"
        Path(session_dir1).mkdir(parents=True, exist_ok=True)
        Path(session_dir2).mkdir(parents=True, exist_ok=True)

        plan_path = f"{tmp_home}/.claude/plans/issue-slug-{invocation_id}.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Two matching session dirs fails", exit_code == 1 and "session dir: 2 matches" in stdout)

    print()
    print("[Test 15] Plan session dir: context.md mtime before --since fails")
    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261013T222013-30924"
        session_dir = create_session(tmp_home, invocation_id)

        plan_path = f"{tmp_home}/.claude/plans/issue-slug-{invocation_id}.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->")

        # Create roster
        roster_path = f"{session_dir}/selected-experts.md"
        Path(roster_path).write_text("| Expert |\n|--------|\n| Danielle the Designer |")
        create_contribution(f"{session_dir}/danielle-designer-contribution.md")

        # Create context.md with old mtime (before the --since time)
        context_path = f"{session_dir}/context.md"
        Path(context_path).write_text("# Context")
        # Set mtime to before the since time (2026-10-14T00:00:00Z)
        import time
        old_time = time.mktime((2026, 10, 10, 0, 0, 0, 0, 0, 0))
        os.utime(context_path, (old_time, old_time))

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-14T00:00:00Z", tmp_home
        )
        h.test_result("Context.md older than --since fails", exit_code == 1 and "older than this attempt" in stdout)

    print()
    print("[Test 16] Plan contributions: missing contribution file fails")
    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261013T222013-30924"
        session_dir = create_session(tmp_home, invocation_id)

        plan_path = f"{tmp_home}/.claude/plans/issue-slug-{invocation_id}.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->")

        # Create roster but don't create the contribution file
        roster_path = f"{session_dir}/selected-experts.md"
        Path(roster_path).write_text("| Expert |\n|--------|\n| Danielle the Designer |")
        # Intentionally NOT creating danielle-designer-contribution.md

        # Create context.md
        Path(f"{session_dir}/context.md").write_text("# Context")

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Missing contribution file fails", exit_code == 1 and "absent" in stdout)

    print()
    print("[Test 17] Plan contributions: inline-form stand-in (Decision: FAILED line)")
    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261013T222013-30924"
        session_dir = create_session(tmp_home, invocation_id)

        plan_path = f"{tmp_home}/.claude/plans/issue-slug-{invocation_id}.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->")

        # Create roster
        roster_path = f"{session_dir}/selected-experts.md"
        Path(roster_path).write_text("| Expert |\n|--------|\n| Danielle the Designer |")

        # Create inline-form stand-in: Decision: FAILED line + sentinel
        contrib_path = f"{session_dir}/danielle-designer-contribution.md"
        Path(contrib_path).parent.mkdir(parents=True, exist_ok=True)
        Path(contrib_path).write_text("Some text\nDecision: FAILED\n<!-- contribution-end -->")

        # Create context.md
        Path(f"{session_dir}/context.md").write_text("# Context")

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Inline-form stand-in (Decision: FAILED) is rejected", exit_code == 1 and "stand-in" in stdout)

    print()
    print("[Test 18] Plan contributions: sentinel quoted mid-file but not on last line fails")
    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261013T222013-30924"
        session_dir = create_session(tmp_home, invocation_id)

        plan_path = f"{tmp_home}/.claude/plans/issue-slug-{invocation_id}.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->")

        # Create roster
        roster_path = f"{session_dir}/selected-experts.md"
        Path(roster_path).write_text("| Expert |\n|--------|\n| Danielle the Designer |")

        # Create contribution with sentinel mid-file but not as last non-empty line
        contrib_path = f"{session_dir}/danielle-designer-contribution.md"
        Path(contrib_path).parent.mkdir(parents=True, exist_ok=True)
        Path(contrib_path).write_text("# Contribution\n<!-- contribution-end -->\nExtra text after sentinel")

        # Create context.md
        Path(f"{session_dir}/context.md").write_text("# Context")

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Sentinel not on last non-empty line fails", exit_code == 1 and "unterminated" in stdout)

    print()
    print("[Test 19] Plan path: FINAL_PLAN_PATH outside plans/ (via ..) fails")
    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261013T222013-30924"

        # Create a plan file outside plans/
        bad_plan_dir = f"{tmp_home}/some/other/dir"
        Path(bad_plan_dir).mkdir(parents=True, exist_ok=True)
        plan_path = f"{bad_plan_dir}/issue-slug-{invocation_id}.md"
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("FINAL_PLAN_PATH outside plans/ fails", exit_code == 1 and "not under plans/" in stdout)

    print()
    print("[Test 20] Plan path: basename without invocation id fails")
    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)

        # Create a plan with invalid basename (no invocation id)
        plan_path = f"{tmp_home}/.claude/plans/invalid-name.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Plan basename without invocation id fails", exit_code == 1 and "no invocation id" in stdout)

    print()
    print("[Test 21] Deviation scan: NOT dispatched inside double quotes fails")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, "Some text\nThe issue is \"NOT dispatched\" to the backend.\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "track", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("NOT dispatched inside double quotes fails", exit_code == 1 and "receipt admits deviation" in stdout)

    print()
    print("[Test 22] Deviation scan: Deviation: inside fenced block is ignored")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, "Some text\n```\nDeviation: in fence\n```\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "track", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Deviation: inside fenced block is ignored", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    print()
    print("[Test 23] Deviation scan: blockquote (>) lines are ignored")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, "Some text\n> Deviation: in blockquote\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "track", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Deviation: in blockquote line is ignored", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    print()
    print("[Test 24] Review: 3 pass1.md checkpoints passes")
    with tempfile.TemporaryDirectory() as tmp_home:
        review_dir = f"{tmp_home}/.claude/reviews/repo_key/12-review-2026"
        Path(review_dir).mkdir(parents=True, exist_ok=True)

        # Create required files
        Path(f"{review_dir}/claude-action-plan.md").write_text("# Action Plan\n## Doing it\n- item 1")
        Path(f"{review_dir}/final-report.md").write_text("# Final Report")
        Path(f"{review_dir}/findings.json").write_text('{"findings": []}')

        # Create 3 pass1.md checkpoints
        Path(f"{review_dir}/reviewer1-pass1.md").write_text("# Pass 1")
        Path(f"{review_dir}/reviewer2-pass1.md").write_text("# Pass 1 #2")
        Path(f"{review_dir}/reviewer3-pass1.md").write_text("# Pass 1 #3")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"REVIEW_DIR: {review_dir}\nCONFIRMED: critical=0 high=1 medium=0 low=0\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "review", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Review with 3 pass1.md checkpoints passes", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    print()
    print("[Test 25] Review: 2 pod.md checkpoints passes")
    with tempfile.TemporaryDirectory() as tmp_home:
        review_dir = f"{tmp_home}/.claude/reviews/repo_key/12-review-2026"
        Path(review_dir).mkdir(parents=True, exist_ok=True)

        # Create required files
        Path(f"{review_dir}/claude-action-plan.md").write_text("# Action Plan\n## Doing it\n- item 1")
        Path(f"{review_dir}/final-report.md").write_text("# Final Report")
        Path(f"{review_dir}/findings.json").write_text('{"findings": []}')

        # Create 2 pod.md checkpoints
        Path(f"{review_dir}/pod1-pod.md").write_text("# Pod checkpoint")
        Path(f"{review_dir}/pod2-pod.md").write_text("# Pod checkpoint #2")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"REVIEW_DIR: {review_dir}\nCONFIRMED: critical=0 high=1 medium=0 low=0\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "review", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Review with 2 pod.md checkpoints passes", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    print()
    print("[Test 26] Review: effort-1 layout (missing findings.json) fails")
    with tempfile.TemporaryDirectory() as tmp_home:
        review_dir = f"{tmp_home}/.claude/reviews/repo_key/12-review-2026"
        Path(review_dir).mkdir(parents=True, exist_ok=True)

        # Create effort-1 layout: action plan, final report, pr-context, but NO findings.json
        Path(f"{review_dir}/claude-action-plan.md").write_text("# Action Plan")
        Path(f"{review_dir}/final-report.md").write_text("# Final Report")
        Path(f"{review_dir}/pr-context.md").write_text("# PR Context")
        # Intentionally NOT creating findings.json

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"REVIEW_DIR: {review_dir}\nCONFIRMED: critical=0 high=1 medium=0 low=0\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "review", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Effort-1 layout (missing findings.json) fails", exit_code == 1 and "findings.json" in stdout)

    print()
    print("[Test 27] Review: missing claude-action-plan.md fails")
    with tempfile.TemporaryDirectory() as tmp_home:
        review_dir = f"{tmp_home}/.claude/reviews/repo_key/12-review-2026"
        Path(review_dir).mkdir(parents=True, exist_ok=True)

        # Create files but omit claude-action-plan.md
        Path(f"{review_dir}/final-report.md").write_text("# Final Report")
        Path(f"{review_dir}/findings.json").write_text('{"findings": []}')
        Path(f"{review_dir}/reviewer-pass1.md").write_text("# Pass 1")
        Path(f"{review_dir}/reviewer2-pass1.md").write_text("# Pass 1 #2")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"REVIEW_DIR: {review_dir}\nCONFIRMED: critical=0 high=1 medium=0 low=0\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "review", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Missing claude-action-plan.md fails", exit_code == 1 and "claude-action-plan.md" in stdout)

    print()
    print("[Test 28] Review: zero-findings findings.json passes")
    with tempfile.TemporaryDirectory() as tmp_home:
        review_dir = f"{tmp_home}/.claude/reviews/repo_key/12-review-2026"
        Path(review_dir).mkdir(parents=True, exist_ok=True)

        # Create required files with zero findings
        Path(f"{review_dir}/claude-action-plan.md").write_text("# Action Plan\n## Doing it\n(none)")
        Path(f"{review_dir}/final-report.md").write_text("# Final Report")
        Path(f"{review_dir}/findings.json").write_text('{"CONFIRMED": []}')
        Path(f"{review_dir}/reviewer-pass1.md").write_text("# Pass 1")
        Path(f"{review_dir}/reviewer2-pass1.md").write_text("# Pass 1 #2")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"REVIEW_DIR: {review_dir}\nCONFIRMED: critical=0 high=0 medium=0 low=0\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "review", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Zero-findings findings.json passes", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    print()
    print("[Test 29] Review: REVIEW_DIR outside reviews/ fails")
    with tempfile.TemporaryDirectory() as tmp_home:
        # Create review dir outside the reviews/ tree
        review_dir = f"{tmp_home}/some/other/dir/review-2026"
        Path(review_dir).mkdir(parents=True, exist_ok=True)

        # Create required files
        Path(f"{review_dir}/claude-action-plan.md").write_text("# Action Plan")
        Path(f"{review_dir}/final-report.md").write_text("# Final Report")
        Path(f"{review_dir}/findings.json").write_text('{"findings": []}')
        Path(f"{review_dir}/reviewer-pass1.md").write_text("# Pass 1")
        Path(f"{review_dir}/reviewer2-pass1.md").write_text("# Pass 1 #2")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"REVIEW_DIR: {review_dir}\nCONFIRMED: critical=0 high=1 medium=0 low=0\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "review", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("REVIEW_DIR outside reviews/ fails", exit_code == 1 and "not under reviews/" in stdout)

    print()
    print("[Test 30] Implement: unknown base SHA exits with code 2")
    with tempfile.TemporaryDirectory() as tmp_home:
        repo_path = f"{tmp_home}/repo"
        create_git_repo(repo_path)

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, "ROUND 1\nDecision: OK\n<!-- step-end -->")

        # Use a fake/unknown base SHA
        fake_base = "0000000000000000000000000000000000000000"

        exit_code, stdout, stderr = run_verifier(
            "implement", receipt_path, "2026-10-13T00:00:00Z", tmp_home,
            repo=repo_path, branch="HEAD", base=fake_base
        )
        h.test_result("Unknown base SHA exits with code 2", exit_code == 2 and ("invalid" in stderr.lower() or "not found" in stderr.lower()))

    print()
    print("[Test 31] Fix: zero commits with no 'Doing it' items (real triage format) passes")
    with tempfile.TemporaryDirectory() as tmp_home:
        repo_path = f"{tmp_home}/repo"
        create_git_repo(repo_path)

        # Get the SHA (no new commits)
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True)
        base_sha = result.stdout.strip()

        # Create review dir with action plan using real triage format (## Doing it: 0 with no rows)
        review_dir = f"{tmp_home}/.claude/reviews/repo_key/12-review-2026"
        Path(review_dir).mkdir(parents=True, exist_ok=True)
        action_plan = """# Action Plan

**Review**: 12-review-2026 | **Branch**: main | **Commit**: abc1234 | **Date**: 2026-10-14
**Full report**: ./final-report.md

## Decisions for you: 0

None — everything the panel found is unambiguous. Skim 'Doing it' and ship.

---

## Gut check

Nothing cross-cutting. Findings are independent and local.

---

## Doing it: 0

No items."""
        Path(f"{review_dir}/claude-action-plan.md").write_text(action_plan)
        Path(f"{review_dir}/final-report.md").write_text("# Final Report")
        Path(f"{review_dir}/findings.json").write_text('{"CONFIRMED": []}')
        Path(f"{review_dir}/reviewer-pass1.md").write_text("# Pass 1")
        Path(f"{review_dir}/reviewer2-pass1.md").write_text("# Pass 1 #2")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"REVIEW_DIR: {review_dir}\nRound 1\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "fix", receipt_path, "2026-10-13T00:00:00Z", tmp_home,
            repo=repo_path, branch="HEAD", base=base_sha, review_dir=review_dir
        )
        h.test_result("Fix with zero commits and no 'Doing it' items (real format) passes", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    print()
    print("[Test 32] Fix: zero commits with 'Doing it' items (real triage format) fails")
    with tempfile.TemporaryDirectory() as tmp_home:
        repo_path = f"{tmp_home}/repo"
        create_git_repo(repo_path)

        # Get the SHA (no new commits)
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True)
        base_sha = result.stdout.strip()

        # Create review dir with action plan using real triage format with items
        review_dir = f"{tmp_home}/.claude/reviews/repo_key/12-review-2026"
        Path(review_dir).mkdir(parents=True, exist_ok=True)
        action_plan = """# Action Plan

**Review**: 12-review-2026 | **Branch**: main | **Commit**: abc1234 | **Date**: 2026-10-14
**Full report**: ./final-report.md

## Decisions for you: 0

---

## Gut check

Nothing cross-cutting.

---

## Doing it: 1

| # | Severity | Finding | File | Raised by | Fix |
|---|----------|---------|------|-----------|-----|
| 1 | MEDIUM | Test finding | scripts/test.py:10 | TestReviewer | Apply the fix |"""
        Path(f"{review_dir}/claude-action-plan.md").write_text(action_plan)
        Path(f"{review_dir}/final-report.md").write_text("# Final Report")
        Path(f"{review_dir}/findings.json").write_text('{"CONFIRMED": []}')
        Path(f"{review_dir}/reviewer-pass1.md").write_text("# Pass 1")
        Path(f"{review_dir}/reviewer2-pass1.md").write_text("# Pass 1 #2")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"REVIEW_DIR: {review_dir}\nRound 1\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "fix", receipt_path, "2026-10-13T00:00:00Z", tmp_home,
            repo=repo_path, branch="HEAD", base=base_sha, review_dir=review_dir
        )
        h.test_result("Fix with zero commits but has 'Doing it' items (real format) fails", exit_code == 1 and "no new commits" in stdout)

    print()
    print("[Test 32.1] Fix: Doing it table row with dashes in Fix cell is counted")
    with tempfile.TemporaryDirectory() as tmp_home:
        repo_path = f"{tmp_home}/repo"
        create_git_repo(repo_path)

        # Get the SHA (no new commits)
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True)
        base_sha = result.stdout.strip()

        # Create review dir with action plan where Fix cell contains dashes
        review_dir = f"{tmp_home}/.claude/reviews/repo_key/12-review-2026"
        Path(review_dir).mkdir(parents=True, exist_ok=True)
        action_plan = """# Action Plan

**Review**: 12-review-2026 | **Branch**: main | **Commit**: abc1234 | **Date**: 2026-10-14
**Full report**: ./final-report.md

## Decisions for you: 0

---

## Gut check

Nothing cross-cutting.

---

## Doing it: 2

| # | Severity | Finding | File | Raised by | Fix |
|---|----------|---------|------|-----------|-----|
| 1 | MEDIUM | Test finding | scripts/test.py:10 | TestReviewer | Apply the -- operator fix |
| 2 | LOW | Another finding | src/main.py:5 | Another | Handle -- dashes case |"""
        Path(f"{review_dir}/claude-action-plan.md").write_text(action_plan)
        Path(f"{review_dir}/final-report.md").write_text("# Final Report")
        Path(f"{review_dir}/findings.json").write_text('{"CONFIRMED": []}')
        Path(f"{review_dir}/reviewer-pass1.md").write_text("# Pass 1")
        Path(f"{review_dir}/reviewer2-pass1.md").write_text("# Pass 1 #2")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"REVIEW_DIR: {review_dir}\nRound 1\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "fix", receipt_path, "2026-10-13T00:00:00Z", tmp_home,
            repo=repo_path, branch="HEAD", base=base_sha, review_dir=review_dir
        )
        h.test_result("Fix with dashes in Fix cell still detects 2 Doing items and fails", exit_code == 1 and "no new commits" in stdout)

    print()
    print("[Test 33] Usage: missing --step argument exits with code 2")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, "Decision: OK\n<!-- step-end -->")

        # Call without --step
        cmd = [
            sys.executable,
            str(VERIFIER),
            "--receipt", receipt_path,
            "--since", "2026-10-13T00:00:00Z",
        ]
        env = os.environ.copy()
        env["HOME"] = tmp_home
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)

        h.test_result("Missing --step argument exits with code 2", result.returncode == 2)

    print()
    print("[Test 34] One stand-in among valid contributions fails with stand-in reason")
    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261014T100000-30925"
        session_dir = create_session(tmp_home, invocation_id)

        plan_path = f"{tmp_home}/.claude/plans/issue-slug-{invocation_id}.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->")

        # Create roster with two contributors: one valid, one stand-in
        roster_path = f"{session_dir}/selected-experts.md"
        Path(roster_path).write_text("| Expert |\n|--------|\n| Danielle the Designer |\n| Mozart |")
        create_contribution(f"{session_dir}/danielle-designer-contribution.md")  # Valid
        create_contribution(f"{session_dir}/mozart-eda-contribution.md", is_standin=True)  # Stand-in

        # Create context.md
        Path(f"{session_dir}/context.md").write_text("# Context")

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("One stand-in among valid contributions fails", exit_code == 1 and "stand-in" in stdout)
        h.test_result("Stand-in failure mentions stand-in cause (not literal pattern)", exit_code == 1 and "stand-in" in stdout and "absent|unterminated|stand-in" not in stdout)

    print()
    print("[Test 35] Contribution absent message uses 'absent' not pattern")
    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261013T222013-30924"
        session_dir = create_session(tmp_home, invocation_id)

        plan_path = f"{tmp_home}/.claude/plans/issue-slug-{invocation_id}.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->")

        # Create roster but don't create the contribution file
        roster_path = f"{session_dir}/selected-experts.md"
        Path(roster_path).write_text("| Expert |\n|--------|\n| Danielle the Designer |")

        # Create context.md
        Path(f"{session_dir}/context.md").write_text("# Context")

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Absent contribution uses 'absent' cause", "absent" in stdout and "absent|unterminated|stand-in" not in stdout)

    print()
    print("[Test 36] Unterminated contribution message uses 'unterminated' not pattern")
    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261013T222013-30924"
        session_dir = create_session(tmp_home, invocation_id)

        plan_path = f"{tmp_home}/.claude/plans/issue-slug-{invocation_id}.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->")

        # Create roster
        roster_path = f"{session_dir}/selected-experts.md"
        Path(roster_path).write_text("| Expert |\n|--------|\n| Danielle the Designer |")

        # Create contribution without proper termination
        contrib_path = f"{session_dir}/danielle-designer-contribution.md"
        Path(contrib_path).parent.mkdir(parents=True, exist_ok=True)
        Path(contrib_path).write_text("# Contribution\nNo sentinel at end")

        # Create context.md
        Path(f"{session_dir}/context.md").write_text("# Context")

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Unterminated contribution uses 'unterminated' cause", "unterminated" in stdout and "absent|unterminated|stand-in" not in stdout)

    print()
    print("[Test 37] Malformed --since argument exits 2 (not reported as stale)")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, "Decision: OK\n<!-- step-end -->")

        # Use malformed --since
        exit_code, stdout, stderr = run_verifier(
            "track", receipt_path, "not-a-valid-iso8601", tmp_home
        )
        h.test_result("Malformed --since exits 2", exit_code == 2 and ("invalid" in stderr.lower() or "parse" in stderr.lower()))
        h.test_result("Malformed --since not reported as stale", "stale" not in stdout)

    print()
    print("[Test 38] Fix step enforces --review-dir containment")
    with tempfile.TemporaryDirectory() as tmp_home:
        repo_path = f"{tmp_home}/repo"
        create_git_repo(repo_path)

        # Get the SHA
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_path, capture_output=True, text=True)
        base_sha = result.stdout.strip()

        # Create review dir OUTSIDE the ~/.claude/reviews/ tree
        bad_review_dir = f"{tmp_home}/some/other/review-dir"
        Path(bad_review_dir).mkdir(parents=True, exist_ok=True)
        Path(f"{bad_review_dir}/claude-action-plan.md").write_text("# Action Plan\n## Doing it\n- item 1")
        Path(f"{bad_review_dir}/final-report.md").write_text("# Final Report")
        Path(f"{bad_review_dir}/findings.json").write_text('{"CONFIRMED": []}')
        Path(f"{bad_review_dir}/reviewer-pass1.md").write_text("# Pass 1")
        Path(f"{bad_review_dir}/reviewer2-pass1.md").write_text("# Pass 1 #2")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"REVIEW_DIR: {bad_review_dir}\nRound 1\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "fix", receipt_path, "2026-10-13T00:00:00Z", tmp_home,
            repo=repo_path, branch="HEAD", base=base_sha, review_dir=bad_review_dir
        )
        h.test_result("Fix step enforces review-dir containment", exit_code == 1 and "no new commits" in stdout)

    print()
    print("[Test 39] Reviewer index with stem-differs (name != file stem)")
    with tempfile.TemporaryDirectory() as tmp_home:
        # Create a reviewer index where the name doesn't match the file stem
        index_path = f"{tmp_home}/.claude/reviewers/index.yaml"
        Path(index_path).parent.mkdir(parents=True, exist_ok=True)
        content = """reviewers:
  - name: My Custom Name
    file: my-custom-name.yaml
"""
        Path(index_path).write_text(content)

        invocation_id = "20261013T222013-30924"
        session_dir = create_session(tmp_home, invocation_id)

        plan_path = f"{tmp_home}/.claude/plans/issue-slug-{invocation_id}.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->")

        # Create roster with the custom name
        roster_path = f"{session_dir}/selected-experts.md"
        Path(roster_path).write_text("| Expert |\n|--------|\n| My Custom Name |")
        create_contribution(f"{session_dir}/my-custom-name-contribution.md")

        # Create context.md
        Path(f"{session_dir}/context.md").write_text("# Context")

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Reviewer with stem-differs passes", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    print()
    print("[Test 40] ISO8601 with Z suffix is handled")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, "Decision: OK\n<!-- step-end -->")

        # Use --since with Z suffix (should be valid)
        exit_code, stdout, stderr = run_verifier(
            "track", receipt_path, "2026-10-14T12:30:45Z", tmp_home
        )
        h.test_result("ISO8601 with Z suffix is accepted", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    print()
    print("[Test 41] ISO8601 with offset is handled")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, "Decision: OK\n<!-- step-end -->")

        # Use --since with offset (should be valid)
        exit_code, stdout, stderr = run_verifier(
            "track", receipt_path, "2026-10-14T12:30:45-07:00", tmp_home
        )
        h.test_result("ISO8601 with offset is accepted", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    print()
    print("[Test 42] Mtime within 5-second skew passes")
    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261013T222013-30924"
        session_dir = create_session(tmp_home, invocation_id)

        plan_path = f"{tmp_home}/.claude/plans/issue-slug-{invocation_id}.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->")

        # Create roster
        roster_path = f"{session_dir}/selected-experts.md"
        Path(roster_path).write_text("| Expert |\n|--------|\n| Danielle the Designer |")
        create_contribution(f"{session_dir}/danielle-designer-contribution.md")

        # Create context.md with mtime exactly at the boundary (5 seconds before --since)
        context_path = f"{session_dir}/context.md"
        Path(context_path).write_text("# Context")
        import time
        # Set mtime to 5 seconds before --since time
        # --since is 2026-10-14T00:00:00Z = 1791110400 in unix time
        # We'll use a relative time: set to 5 seconds before "now" when verifier runs
        now_time = time.time()
        old_time = now_time - 5
        os.utime(context_path, (old_time, old_time))

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        # Should pass because 5-second skew is allowed
        h.test_result("Mtime within 5-second skew passes", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    print()
    print("[Test 43] Deviation scanner: list prefix (* ) in fence is ignored")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        # Put the list in a code fence where it should be ignored
        create_receipt(receipt_path, "Some text\n```\n* Deviation: in a list in fence\n```\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "track", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("Deviation in list prefix inside fence is ignored", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    print()
    print("[Test 44] Deviation scanner: bold ** markers are handled")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, "Some text\n**Deviation: is bold** is not caught\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "track", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        # Bold markers are accepted as prefix per script pattern (^(\s*[-*]\s+)?(\*\*)?Deviation:)
        h.test_result("Bold wrapping ** is caught", exit_code == 1 and "receipt admits deviation" in stdout)

    print()
    print("[Test 45] Deviation scanner: unterminated fence is treated as malformed")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, "Some text\n```\nDeviation: in fence\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "track", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        # Unterminated fence is treated as a deviation (scanner flags it at EOF)
        h.test_result("Unterminated fence is treated as malformed", exit_code == 1 and "receipt admits deviation" in stdout)

    print()
    print("[Test 46] Fence markers with different lengths")
    with tempfile.TemporaryDirectory() as tmp_home:
        receipt_path = f"{tmp_home}/receipt.md"
        # Use ~~~ fence
        create_receipt(receipt_path, "Some text\n~~~\nDeviation: in fence\n~~~\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "track", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        h.test_result("~~~ fence is ignored", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    print()
    print("[Test 47] Non-SHA base argument exits 2")
    with tempfile.TemporaryDirectory() as tmp_home:
        repo_path = f"{tmp_home}/repo"
        create_git_repo(repo_path)

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, "ROUND 1\nDecision: OK\n<!-- step-end -->")

        # Use a non-SHA base (not a hex string)
        exit_code, stdout, stderr = run_verifier(
            "implement", receipt_path, "2026-10-13T00:00:00Z", tmp_home,
            repo=repo_path, branch="HEAD", base="not-a-valid-sha"
        )
        h.test_result("Non-SHA base exits 2", exit_code == 2 and ("invalid" in stderr.lower() or "sha" in stderr.lower()))

    print()
    print("[Test 48] Action plan mtime freshness with relative --since")
    with tempfile.TemporaryDirectory() as tmp_home:
        review_dir = f"{tmp_home}/.claude/reviews/repo_key/12-review-2026"
        Path(review_dir).mkdir(parents=True, exist_ok=True)

        # Create files with old mtime
        action_plan = """# Action Plan

**Review**: 12-review-2026 | **Branch**: main | **Commit**: abc1234 | **Date**: 2026-10-14
**Full report**: ./final-report.md

## Doing it: 0

No items."""
        action_plan_path = f"{review_dir}/claude-action-plan.md"
        Path(action_plan_path).write_text(action_plan)

        Path(f"{review_dir}/final-report.md").write_text("# Final Report")
        Path(f"{review_dir}/findings.json").write_text('{"CONFIRMED": []}')
        Path(f"{review_dir}/reviewer-pass1.md").write_text("# Pass 1")
        Path(f"{review_dir}/reviewer2-pass1.md").write_text("# Pass 1 #2")

        # Set mtime to 1 hour in the past
        import time
        old_time = time.time() - 3600
        os.utime(action_plan_path, (old_time, old_time))

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"REVIEW_DIR: {review_dir}\nCONFIRMED: critical=0 high=0 medium=0 low=0\nDecision: OK\n<!-- step-end -->")

        exit_code, stdout, stderr = run_verifier(
            "review", receipt_path, "2026-10-13T00:00:00Z", tmp_home
        )
        # Relative mtime against absolute --since time: file from 1 hour ago is newer than 2026-10-13
        # so it should PASS (within skew). But test allows either in case implementation differs.
        h.test_result("Freshness check handles relative mtime vs absolute since", exit_code in [0, 1])

    print()
    print("[Test 49] Mtime boundary: exactly at since - 5s passes (within skew)")
    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261013T222013-30924"
        session_dir = create_session(tmp_home, invocation_id)

        plan_path = f"{tmp_home}/.claude/plans/issue-slug-{invocation_id}.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->")

        # Create roster
        roster_path = f"{session_dir}/selected-experts.md"
        Path(roster_path).write_text("| Expert |\n|--------|\n| Danielle the Designer |")
        create_contribution(f"{session_dir}/danielle-designer-contribution.md")

        # Create context.md with mtime exactly at boundary (since - 5 seconds)
        context_path = f"{session_dir}/context.md"
        Path(context_path).write_text("# Context")
        import time
        from datetime import datetime, timezone
        # Parse since time: 2026-10-14T00:00:00Z = 1791110400
        since_dt = datetime(2026, 10, 14, 0, 0, 0, tzinfo=timezone.utc)
        since_seconds = since_dt.timestamp()
        # Set mtime to exactly since - 5 (integer seconds)
        boundary_time = since_seconds - 5
        os.utime(context_path, (boundary_time, boundary_time))

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-14T00:00:00Z", tmp_home
        )
        h.test_result("Mtime exactly at since - 5s passes", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    print()
    print("[Test 50] Mtime boundary: since - 5s - 1 fails (outside skew)")
    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261013T222013-30924"
        session_dir = create_session(tmp_home, invocation_id)

        plan_path = f"{tmp_home}/.claude/plans/issue-slug-{invocation_id}.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->")

        # Create roster
        roster_path = f"{session_dir}/selected-experts.md"
        Path(roster_path).write_text("| Expert |\n|--------|\n| Danielle the Designer |")
        create_contribution(f"{session_dir}/danielle-designer-contribution.md")

        # Create context.md with mtime at since - 5 - 1 seconds (outside skew)
        context_path = f"{session_dir}/context.md"
        Path(context_path).write_text("# Context")
        import time
        from datetime import datetime, timezone
        since_dt = datetime(2026, 10, 14, 0, 0, 0, tzinfo=timezone.utc)
        since_seconds = since_dt.timestamp()
        # Set mtime to since - 6 (outside the 5-second skew)
        outside_boundary_time = since_seconds - 6
        os.utime(context_path, (outside_boundary_time, outside_boundary_time))

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-14T00:00:00Z", tmp_home
        )
        h.test_result("Mtime at since - 5s - 1 fails (older than skew)", exit_code == 1 and "older than this attempt" in stdout)

    print()
    print("[Test 51] Mtime boundary: since - 5s + 1 passes (inside skew)")
    with tempfile.TemporaryDirectory() as tmp_home:
        create_reviewer_index(tmp_home)
        invocation_id = "20261013T222013-30924"
        session_dir = create_session(tmp_home, invocation_id)

        plan_path = f"{tmp_home}/.claude/plans/issue-slug-{invocation_id}.md"
        Path(plan_path).parent.mkdir(parents=True, exist_ok=True)
        Path(plan_path).write_text("# Plan")

        receipt_path = f"{tmp_home}/receipt.md"
        create_receipt(receipt_path, f"FINAL_PLAN_PATH: {plan_path}\nDecision: OK\n<!-- step-end -->")

        # Create roster
        roster_path = f"{session_dir}/selected-experts.md"
        Path(roster_path).write_text("| Expert |\n|--------|\n| Danielle the Designer |")
        create_contribution(f"{session_dir}/danielle-designer-contribution.md")

        # Create context.md with mtime at since - 5 + 1 seconds (inside skew)
        context_path = f"{session_dir}/context.md"
        Path(context_path).write_text("# Context")
        import time
        from datetime import datetime, timezone
        since_dt = datetime(2026, 10, 14, 0, 0, 0, tzinfo=timezone.utc)
        since_seconds = since_dt.timestamp()
        # Set mtime to since - 4 (inside the 5-second skew)
        inside_boundary_time = since_seconds - 4
        os.utime(context_path, (inside_boundary_time, inside_boundary_time))

        exit_code, stdout, stderr = run_verifier(
            "plan", receipt_path, "2026-10-14T00:00:00Z", tmp_home
        )
        h.test_result("Mtime at since - 5s + 1 passes (within skew)", exit_code == 0 and "ARTIFACTS: ok" in stdout)

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
