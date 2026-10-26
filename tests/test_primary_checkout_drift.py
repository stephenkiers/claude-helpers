#!/usr/bin/env python3
"""
Test suite for scripts/primary-checkout-drift.py

Tests cover: snapshot capture, compare detection, exit codes, FAIL/WARN/INFO classification,
ticket-match/differs/absent, planned-set filtering, skipping, and data integrity.

Run with: python3 tests/test_primary_checkout_drift.py
"""

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _test_harness import Harness, REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import telemetry_schema

DRIFT_SCRIPT = REPO_ROOT / "scripts" / "primary-checkout-drift.py"


def init_git_repo(repo_path: str) -> str:
    """Initialize a git repo and return the HEAD commit SHA."""
    Path(repo_path).mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=repo_path, capture_output=True, check=True)
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
    Path(repo_path) / "initial.txt"
    Path(repo_path, "initial.txt").write_text("initial content")
    subprocess.run(
        ["git", "add", "initial.txt"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )
    result = subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=repo_path,
        capture_output=True,
        check=True,
    )

    # Get HEAD
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

    # Get HEAD in the new worktree
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def run_drift_script(cmd: str, *args: str) -> tuple:
    """Run the drift script and return (exit_code, stdout, stderr)."""
    result = subprocess.run(
        [sys.executable, str(DRIFT_SCRIPT), cmd] + list(args),
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout, result.stderr


def check_git_index_lock(repo_path: str) -> bool:
    """Return True if .git/index.lock exists, False otherwise."""
    return Path(repo_path, ".git", "index.lock").exists()


def get_git_index_mtime(repo_path: str) -> float:
    """Get .git/index mtime, return -1 if not found."""
    index_path = Path(repo_path, ".git", "index")
    if index_path.exists():
        return index_path.stat().st_mtime
    return -1


def get_git_status_output(repo_path: str) -> bytes:
    """Get raw git status --porcelain=v1 -z output."""
    result = subprocess.run(
        ["git", "-C", repo_path, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        capture_output=True,
    )
    return result.stdout


def main():
    h = Harness("PRIMARY-CHECKOUT-DRIFT TEST SUITE")

    # Test 1: cwd_drift in telemetry_schema.FAILURE_CLASSES
    print("[Test 1] cwd_drift is in telemetry_schema.FAILURE_CLASSES")
    h.test_result(
        "cwd_drift in FAILURE_CLASSES",
        "cwd_drift" in telemetry_schema.FAILURE_CLASSES,
        f"FAILURE_CLASSES={telemetry_schema.FAILURE_CLASSES}",
    )
    print()

    # Test 2: snapshot + compare clean (no changes)
    print("[Test 2] Clean primary: snapshot + compare exit 0, no output")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        # Snapshot
        exit_code, stdout, stderr = run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )
        h.test_result(
            "snapshot exit 0",
            exit_code == 0,
            f"exit={exit_code}, stderr={stderr}",
        )

        # Compare
        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "compare clean exit 0",
            exit_code == 0 and "FAIL" not in stdout,
            f"exit={exit_code}, stdout={stdout[:200]}",
        )
    print()

    # Test 3: New untracked planned file in primary
    print("[Test 3] New untracked planned file in primary -> FAIL ticket-absent")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("newfile.txt\n")

        # Snapshot
        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Create new file in primary
        Path(primary_path, "newfile.txt").write_text("new content")

        # Compare
        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "new planned file -> FAIL",
            exit_code == 1 and "FAIL newfile.txt ticket-absent" in stdout,
            f"exit={exit_code}, stdout={stdout[:300]}",
        )
    print()

    # Test 4: Planned file with matching content -> ticket-match
    print("[Test 4] Planned file with byte-identical content -> FAIL ticket-match")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))

        # Create file in ticket worktree
        Path(ticket_path, "samefile.txt").write_text("same content")

        planned_path.write_text("samefile.txt\n")

        # Snapshot
        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Create matching file in primary
        Path(primary_path, "samefile.txt").write_text("same content")

        # Compare
        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "planned file with matching content -> ticket-match",
            exit_code == 1 and "FAIL samefile.txt ticket-match" in stdout,
            f"exit={exit_code}, stdout={stdout[:300]}",
        )
    print()

    # Test 5: Planned file with different content -> ticket-differs
    print("[Test 5] Planned file with different content -> FAIL ticket-differs")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))

        # Create file in ticket worktree
        Path(ticket_path, "difffile.txt").write_text("content in ticket")

        planned_path.write_text("difffile.txt\n")

        # Snapshot
        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Create different file in primary
        Path(primary_path, "difffile.txt").write_text("different content in primary")

        # Compare
        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "planned file with different content -> ticket-differs",
            exit_code == 1 and "FAIL difffile.txt ticket-differs" in stdout,
            f"exit={exit_code}, stdout={stdout[:300]}",
        )
    print()

    # Test 6: Unplanned new file -> WARN, exit 0
    print("[Test 6] Unplanned new file -> WARN, exit 0")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))

        planned_path.write_text("")

        # Snapshot
        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Create unplanned file in primary
        Path(primary_path, "unplanned.txt").write_text("unplanned")

        # Compare
        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "unplanned new file -> WARN, exit 0",
            exit_code == 0 and "WARN unplanned.txt" in stdout,
            f"exit={exit_code}, stdout={stdout[:300]}",
        )
    print()

    # Test 7: Pre-dirty planned file unchanged -> WARN pre-dirty-planned
    print("[Test 7] Pre-dirty planned file with same status -> WARN pre-dirty-planned")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))

        # Create file in primary BEFORE snapshot
        Path(primary_path, "predirty.txt").write_text("predirty content")

        planned_path.write_text("predirty.txt\n")

        # Snapshot (file already dirty)
        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # File still dirty with same content
        # (no change between snapshot and compare)

        # Compare
        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "pre-dirty planned unchanged -> WARN pre-dirty-planned",
            exit_code == 0 and "WARN pre-dirty-planned predirty.txt" in stdout,
            f"exit={exit_code}, stdout={stdout[:300]}",
        )
    print()

    # Test 8: HEAD moved -> INFO head-changed
    print("[Test 8] HEAD moved in primary -> INFO head-changed, exit 0")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))

        planned_path.write_text("")

        # Snapshot
        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Move HEAD in primary
        new_file = Path(primary_path, "newcommit.txt")
        new_file.write_text("new commit content")
        subprocess.run(
            ["git", "add", "newcommit.txt"],
            cwd=str(primary_path),
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "new commit"],
            cwd=str(primary_path),
            capture_output=True,
            text=True,
            check=True,
        )

        # Compare
        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "HEAD moved -> INFO head-changed, exit 0",
            exit_code == 0 and "INFO head-changed" in stdout,
            f"exit={exit_code}, stdout={stdout[:300]}",
        )
    print()

    # Test 9: Orchestrator cwd == primary checkout -> SKIP
    print("[Test 9] Orchestrator cwd equals primary -> SKIP in both snapshot and compare")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        planned_path.write_text("")

        # Snapshot with orchestrator_cwd == checkout
        exit_code, stdout, stderr = run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(primary_path),
            "--out", str(baseline_path),
        )
        h.test_result(
            "snapshot: primary == cwd -> SKIP, exit 0",
            exit_code == 0 and "SKIP" in stdout and "primary-is-orchestrator-cwd" in stdout,
            f"exit={exit_code}, stdout={stdout[:300]}",
        )

        # Compare should also skip
        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(primary_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "compare: skipped checkout -> exit 0",
            exit_code == 0,
            f"exit={exit_code}, stdout={stdout[:300]}",
        )
    print()

    # Test 10: Symlinked path equals primary
    print("[Test 10] Symlinked orchestrator cwd equals primary (via symlink) -> SKIP")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        symlink_path = Path(tmpdir) / "symlink_to_primary"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        symlink_path.symlink_to(primary_path)
        planned_path.write_text("")

        # Snapshot with symlink as orchestrator_cwd
        exit_code, stdout, stderr = run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(symlink_path),
            "--out", str(baseline_path),
        )
        h.test_result(
            "snapshot: primary == cwd via symlink -> SKIP, exit 0",
            exit_code == 0 and "SKIP" in stdout,
            f"exit={exit_code}, stdout={stdout[:300]}",
        )
    print()

    # Test 11: Missing baseline file -> exit 2
    print("[Test 11] Missing baseline file -> exit 2 with baseline-unavailable REASON")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "nonexistent.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        # Compare with missing baseline
        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "missing baseline -> exit 2",
            exit_code == 2 and "baseline-unavailable" in stderr,
            f"exit={exit_code}, stderr={stderr[:300]}",
        )
    print()

    # Test 12: Corrupt baseline JSON -> exit 2
    print("[Test 12] Corrupt baseline JSON -> exit 2 with baseline-unavailable")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "corrupt.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))

        baseline_path.write_text("{ invalid json")
        planned_path.write_text("")

        # Compare with corrupt baseline
        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "corrupt baseline -> exit 2",
            exit_code == 2 and "baseline-unavailable" in stderr,
            f"exit={exit_code}, stderr={stderr[:300]}",
        )
    print()

    # Test 13: No .git/index.lock left behind
    print("[Test 13] No .git/index.lock left after snapshot and compare")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        # Snapshot
        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        lock_exists_after_snapshot = check_git_index_lock(str(primary_path))

        # Compare
        run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )

        lock_exists_after_compare = check_git_index_lock(str(primary_path))

        h.test_result(
            "no .git/index.lock after snapshot and compare",
            not lock_exists_after_snapshot and not lock_exists_after_compare,
            f"after_snapshot={lock_exists_after_snapshot}, after_compare={lock_exists_after_compare}",
        )
    print()

    # Test 14: Index file unchanged by snapshot and compare
    print("[Test 14] .git/index mtime unchanged after snapshot and compare")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        initial_mtime = get_git_index_mtime(str(primary_path))

        # Snapshot
        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        mtime_after_snapshot = get_git_index_mtime(str(primary_path))

        # Compare
        run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )

        mtime_after_compare = get_git_index_mtime(str(primary_path))

        h.test_result(
            ".git/index mtime unchanged",
            initial_mtime == mtime_after_snapshot == mtime_after_compare,
            f"initial={initial_mtime}, after_snapshot={mtime_after_snapshot}, after_compare={mtime_after_compare}",
        )
    print()

    # Test 15: Primary checkout status unchanged by snapshot and compare
    print("[Test 15] Primary checkout git status unchanged after snapshot and compare")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        # Create some status in primary
        Path(primary_path, "modified.txt").write_text("original")
        subprocess.run(
            ["git", "add", "modified.txt"],
            cwd=str(primary_path),
            capture_output=True,
            check=True,
        )
        Path(primary_path, "modified.txt").write_text("modified")
        Path(primary_path, "untracked.txt").write_text("untracked")

        initial_status = get_git_status_output(str(primary_path))

        # Snapshot
        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        status_after_snapshot = get_git_status_output(str(primary_path))

        # Compare
        run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )

        status_after_compare = get_git_status_output(str(primary_path))

        h.test_result(
            "primary checkout git status unchanged",
            initial_status == status_after_snapshot == status_after_compare,
            f"lengths: initial={len(initial_status)}, after_snapshot={len(status_after_snapshot)}, after_compare={len(status_after_compare)}",
        )
    print()

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
