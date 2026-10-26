#!/usr/bin/env python3
"""
Spec-blind test suite for scripts/primary-checkout-drift.py CLI contract.

Based on the plan (Step 4: Add the primary-checkout drift helper script),
tested against real temporary git repos without reading the implementation.

Tests cover:
- snapshot subcommand with --checkout, --orchestrator-cwd, --out
- compare subcommand with --baseline, --ticket-wt, --start-sha, --planned
- planned-vs-unplanned classification and exit codes
- pre-dirty baseline handling
- HEAD movement detection (INFO only, not a FAIL)
- skip condition when orchestrator cwd == primary
- missing/corrupt baseline → exit 2
- file integrity: primary checkout never modified

Run with: python3 tests/test_primary_checkout_drift_blackbox.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

DRIFT_SCRIPT = REPO_ROOT / "scripts" / "primary-checkout-drift.py"


def run_script(cmd: str, *args: str) -> tuple:
    """Run the drift script and return (exit_code, stdout, stderr)."""
    result = subprocess.run(
        [sys.executable, str(DRIFT_SCRIPT), cmd] + list(args),
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def init_git_repo(repo_path: str) -> str:
    """Initialize a git repo, return the HEAD commit SHA."""
    Path(repo_path).mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )

    # Create initial commit
    Path(repo_path, "initial.txt").write_text("initial content")
    subprocess.run(
        ["git", "add", "initial.txt"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def create_worktree(primary_path: str, worktree_path: str, branch_name: str = "feature") -> str:
    """Create a worktree from primary. Return HEAD commit SHA."""
    subprocess.run(
        ["git", "worktree", "add", worktree_path, "-b", branch_name],
        cwd=primary_path,
        capture_output=True,
        check=True,
    )

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def get_git_status_output(repo_path: str) -> bytes:
    """Get raw git status --porcelain=v1 -z output for comparison."""
    result = subprocess.run(
        ["git", "-C", repo_path, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        capture_output=True,
    )
    return result.stdout


def get_index_mtime(repo_path: str) -> float:
    """Get .git/index mtime; return -1 if not found."""
    index_path = Path(repo_path, ".git", "index")
    if index_path.exists():
        return index_path.stat().st_mtime
    return -1


def main():
    h = Harness("PRIMARY-CHECKOUT-DRIFT BLACKBOX TEST SUITE")

    # ========================================================================
    # Test Suite 1: snapshot subcommand
    # ========================================================================
    print("[Suite 1] SNAPSHOT subcommand")
    print()

    print("Test 1a: snapshot with clean primary checkout -> exit 0, JSON written")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"

        start_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))

        exit_code, stdout, stderr = run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        h.test_result(
            "snapshot exits 0 when primary is clean",
            exit_code == 0,
            f"exit_code={exit_code}, stderr={stderr}",
        )

        h.test_result(
            "snapshot writes a JSON file",
            baseline_path.exists(),
            "baseline.json not created",
        )

        if baseline_path.exists():
            try:
                data = json.loads(baseline_path.read_text())
                h.test_result(
                    "baseline JSON is valid",
                    isinstance(data, dict),
                    f"Expected dict, got {type(data).__name__}",
                )
            except json.JSONDecodeError as e:
                h.test_result("baseline JSON is valid", False, str(e))

    print()

    print("Test 1b: snapshot with untracked file in primary -> exit 0, JSON written")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"

        init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))

        # Add untracked file to primary
        Path(primary_path, "untracked.txt").write_text("untracked")

        exit_code, stdout, stderr = run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        h.test_result(
            "snapshot still exits 0 with untracked file in primary",
            exit_code == 0,
            f"exit_code={exit_code}",
        )

    print()

    print("Test 1c: snapshot when orchestrator_cwd == primary -> SKIP, exit 0")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        baseline_path = Path(tmpdir) / "baseline.json"

        init_git_repo(str(primary_path))

        exit_code, stdout, stderr = run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(primary_path),
            "--out", str(baseline_path),
        )

        h.test_result(
            "snapshot exits 0 when orchestrator_cwd == primary",
            exit_code == 0,
            f"exit_code={exit_code}",
        )

        h.test_result(
            "snapshot prints SKIP: when orchestrator_cwd == primary",
            "SKIP:" in stdout,
            f"stdout={stdout}",
        )

    print()

    print("Test 1d: snapshot when orchestrator_cwd == primary (symlinked path)")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        symlink_path = Path(tmpdir) / "symlink"
        baseline_path = Path(tmpdir) / "baseline.json"

        init_git_repo(str(primary_path))
        symlink_path.symlink_to(primary_path.resolve())

        exit_code, stdout, stderr = run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(symlink_path),
            "--out", str(baseline_path),
        )

        h.test_result(
            "snapshot detects primary==orchestrator via symlink and prints SKIP:",
            "SKIP:" in stdout and exit_code == 0,
            f"exit_code={exit_code}, stdout={stdout}",
        )

    print()

    print("Test 1e: snapshot with git error (non-repo path) -> exit non-zero")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        non_repo = Path(tmpdir) / "not_a_repo"

        non_repo.mkdir()

        init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))

        exit_code, stdout, stderr = run_script(
            "snapshot",
            "--checkout", str(non_repo),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        h.test_result(
            "snapshot exits non-zero when checkout path is not a git repo",
            exit_code != 0,
            f"exit_code={exit_code}",
        )

        h.test_result(
            "snapshot does not write JSON on git error",
            not baseline_path.exists(),
            "baseline.json should not be created on error",
        )

    print()

    # ========================================================================
    # Test Suite 2: compare subcommand
    # ========================================================================
    print("[Suite 2] COMPARE subcommand")
    print()

    print("Test 2a: compare with clean primary (baseline matches current) -> exit 0")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        start_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        # Take baseline
        run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Compare without changes
        exit_code, stdout, stderr = run_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", start_sha,
            "--planned", str(planned_path),
        )

        h.test_result(
            "compare exits 0 when primary is clean",
            exit_code == 0,
            f"exit_code={exit_code}",
        )

    print()

    print("Test 2b: planned file drift in primary (new) -> exit 1, FAIL <path> ticket-absent")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        start_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("planned_file.txt\n")

        # Take baseline (clean)
        run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Create planned file in primary only
        Path(primary_path, "planned_file.txt").write_text("content")

        # Compare
        exit_code, stdout, stderr = run_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", start_sha,
            "--planned", str(planned_path),
        )

        h.test_result(
            "compare exits 1 when planned file drifts into primary",
            exit_code == 1,
            f"exit_code={exit_code}",
        )

        h.test_result(
            "compare output contains FAIL with planned file path",
            "FAIL" in stdout and "planned_file.txt" in stdout,
            f"stdout={stdout}",
        )

        h.test_result(
            "compare reports ticket-absent status",
            "FAIL" in stdout and "ticket-absent" in stdout,
            f"stdout={stdout}",
        )

    print()

    print("Test 2c: unplanned file drift in primary -> exit 0 with WARN")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        start_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")  # Empty planned list

        # Take baseline
        run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Create unplanned file in primary
        Path(primary_path, "unplanned.txt").write_text("content")

        # Compare
        exit_code, stdout, stderr = run_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", start_sha,
            "--planned", str(planned_path),
        )

        h.test_result(
            "compare exits 0 when unplanned file drifts into primary",
            exit_code == 0,
            f"exit_code={exit_code}",
        )

        h.test_result(
            "compare outputs WARN for unplanned file",
            "WARN" in stdout,
            f"stdout={stdout}",
        )

    print()

    print("Test 2d: planned file with ticket-match content -> FAIL ticket-match")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        start_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("matching.txt\n")

        # Create matching file in ticket
        Path(ticket_path, "matching.txt").write_text("identical content")

        # Take baseline
        run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Create same file in primary with same content
        Path(primary_path, "matching.txt").write_text("identical content")

        # Compare
        exit_code, stdout, stderr = run_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", start_sha,
            "--planned", str(planned_path),
        )

        h.test_result(
            "compare exits 1 for planned file drift even when content matches",
            exit_code == 1,
            f"exit_code={exit_code}",
        )

        h.test_result(
            "compare reports ticket-match status",
            "ticket-match" in stdout,
            f"stdout={stdout}",
        )

    print()

    print("Test 2e: planned file with different content in ticket -> ticket-differs")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        start_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("differs.txt\n")

        # Create file in ticket with one content
        Path(ticket_path, "differs.txt").write_text("ticket content")

        # Take baseline
        run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Create file in primary with different content
        Path(primary_path, "differs.txt").write_text("primary content")

        # Compare
        exit_code, stdout, stderr = run_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", start_sha,
            "--planned", str(planned_path),
        )

        h.test_result(
            "compare exits 1 for drift with different content",
            exit_code == 1,
            f"exit_code={exit_code}",
        )

        h.test_result(
            "compare reports ticket-differs status",
            "ticket-differs" in stdout,
            f"stdout={stdout}",
        )

    print()

    print("Test 2f: pre-dirty planned path -> WARN pre-dirty-planned")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        start_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("dirty.txt\n")

        # Create file in primary before baseline
        Path(primary_path, "dirty.txt").write_text("pre-existing")

        # Take baseline (file already dirty)
        run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # File still exists and unchanged
        # Compare
        exit_code, stdout, stderr = run_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", start_sha,
            "--planned", str(planned_path),
        )

        h.test_result(
            "compare exits 0 when pre-dirty planned file is unchanged",
            exit_code == 0,
            f"exit_code={exit_code}",
        )

        h.test_result(
            "compare warns with pre-dirty-planned for unchanged pre-existing file",
            "WARN pre-dirty-planned" in stdout,
            f"stdout={stdout}",
        )

    print()

    print("Test 2g: HEAD moved by commit in primary -> INFO head-changed, not FAIL")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        start_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        # Take baseline
        run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Make a commit in primary (moving HEAD)
        Path(primary_path, "new.txt").write_text("new")
        subprocess.run(
            ["git", "add", "new.txt"],
            cwd=primary_path,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "new commit"],
            cwd=primary_path,
            capture_output=True,
            check=True,
        )

        # Compare
        exit_code, stdout, stderr = run_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", start_sha,
            "--planned", str(planned_path),
        )

        h.test_result(
            "compare exits 0 even when HEAD moves",
            exit_code == 0,
            f"exit_code={exit_code}",
        )

        h.test_result(
            "compare reports HEAD change with INFO",
            "INFO head-changed" in stdout,
            f"stdout={stdout}",
        )

        h.test_result(
            "HEAD move does not cause FAIL",
            "FAIL" not in stdout,
            f"stdout={stdout}",
        )

    print()

    print("Test 2h: missing baseline file -> exit 2")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "nonexistent.json"
        planned_path = Path(tmpdir) / "planned.txt"

        start_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        # Compare with nonexistent baseline
        exit_code, stdout, stderr = run_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", start_sha,
            "--planned", str(planned_path),
        )

        h.test_result(
            "compare exits 2 with missing baseline",
            exit_code == 2,
            f"exit_code={exit_code}",
        )

    print()

    print("Test 2i: corrupt baseline JSON -> exit 2")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "corrupt.json"
        planned_path = Path(tmpdir) / "planned.txt"

        start_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        # Write corrupt JSON
        baseline_path.write_text("not valid json {{{")

        # Compare with corrupt baseline
        exit_code, stdout, stderr = run_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", start_sha,
            "--planned", str(planned_path),
        )

        h.test_result(
            "compare exits 2 with corrupt baseline JSON",
            exit_code == 2,
            f"exit_code={exit_code}",
        )

    print()

    # ========================================================================
    # Test Suite 3: Data Integrity and Side Effects
    # ========================================================================
    print("[Suite 3] DATA INTEGRITY: Primary checkout never modified")
    print()

    print("Test 3a: snapshot does not modify primary's git state")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"

        init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))

        # Get initial index mtime
        # Plain `git status` may refresh the index itself, so read the mtime after it.
        status_before = get_git_status_output(str(primary_path))
        index_mtime_before = get_index_mtime(str(primary_path))

        # Run snapshot
        run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Get post-snapshot index and status
        index_mtime_after = get_index_mtime(str(primary_path))
        status_after = get_git_status_output(str(primary_path))

        h.test_result(
            "snapshot does not modify .git/index",
            index_mtime_before == index_mtime_after,
            f"index mtime changed: {index_mtime_before} -> {index_mtime_after}",
        )

        h.test_result(
            "snapshot does not change git status output",
            status_before == status_after,
            "git status output changed",
        )

    print()

    print("Test 3b: compare does not create .git/index.lock")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        start_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        # Take baseline
        run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Create a file
        Path(primary_path, "file.txt").write_text("content")

        # Run compare
        run_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", start_sha,
            "--planned", str(planned_path),
        )

        # Check .git/index.lock does not exist
        lock_exists = Path(primary_path, ".git", "index.lock").exists()
        h.test_result(
            "compare does not leave .git/index.lock",
            not lock_exists,
            ".git/index.lock was left behind",
        )

    print()

    print("Test 3c: compare does not modify primary's git state")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        start_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        # Take baseline
        run_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Create a drift file
        Path(primary_path, "drift.txt").write_text("content")

        # Get status before compare
        status_before = get_git_status_output(str(primary_path))

        # Run compare (will detect drift)
        run_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", start_sha,
            "--planned", str(planned_path),
        )

        # Get status after compare
        status_after = get_git_status_output(str(primary_path))

        h.test_result(
            "compare does not modify primary's git state (status unchanged)",
            status_before == status_after,
            "primary git status was modified by compare",
        )

    print()

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
