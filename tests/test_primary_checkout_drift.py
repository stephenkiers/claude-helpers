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
from _inproc import run_python, RepoTemplate

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import telemetry_schema

DRIFT_SCRIPT = REPO_ROOT / "scripts" / "primary-checkout-drift.py"


def _init_git_repo_uncached(root: Path) -> str:
    """Initialize a git repo and return the HEAD commit SHA."""
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=root,
        capture_output=True,
        check=True,
    )

    # Create initial commit
    (root / "initial.txt").write_text("initial content")
    subprocess.run(
        ["git", "add", "initial.txt"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    result = subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=root,
        capture_output=True,
        check=True,
    )

    # Get HEAD
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


_INIT = RepoTemplate(lambda root: _init_git_repo_uncached(root))


def init_git_repo(repo_path: str) -> str:
    """Initialize a git repo and return the HEAD commit SHA."""
    p = Path(repo_path)
    if p.exists():
        p.rmdir()  # callers pass a fresh path; rmdir fails loudly if it is not empty
    _INIT.copy(p)
    return _INIT.meta


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


def _git(cwd, *args: str) -> str:
    """Run a git command in cwd (check=True) and return stripped stdout."""
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def attach_upstream(primary_path, tmpdir) -> str:
    """Give primary a bare origin, push its current branch, set it as upstream.

    Returns the bare origin path (as a string).
    """
    origin = Path(tmpdir) / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], capture_output=True, check=True)
    branch = _git(primary_path, "branch", "--show-current")
    _git(primary_path, "remote", "add", "origin", str(origin))
    _git(primary_path, "push", "-u", "origin", branch)
    return str(origin)


def land_commit_on_upstream(primary_path, origin: str, tmpdir) -> None:
    """Commit in a second clone, push to origin, then fast-forward primary onto it."""
    other = Path(tmpdir) / "other"
    subprocess.run(["git", "clone", origin, str(other)], capture_output=True, check=True)
    _git(other, "config", "user.email", "test@example.com")
    _git(other, "config", "user.name", "Test User")
    (other / "upstream.txt").write_text("landed upstream")
    _git(other, "add", "upstream.txt")
    _git(other, "commit", "-m", "upstream commit")
    _git(other, "push", "origin", "HEAD")
    branch = _git(primary_path, "branch", "--show-current")
    _git(primary_path, "pull", "--ff-only", "origin", branch)


def run_drift_script(cmd: str, *args: str) -> tuple:
    """Run the drift script in-process and return (exit_code, stdout, stderr)."""
    result = run_python([str(DRIFT_SCRIPT), cmd] + list(args))
    return result.returncode, result.stdout, result.stderr


def run_drift_script_e2e(cmd: str, *args: str) -> tuple:
    """Run the drift script as a real subprocess and return (exit_code, stdout, stderr)."""
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

    # Test 8: HEAD moved. Benign only with positive evidence (#495, Q1=B): a fast-forward
    # onto the primary's upstream is not drift; a local commit not on the upstream is.
    print("[Test 8a] HEAD fast-forwarded onto the primary's upstream -> benign, exit 0")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        origin = attach_upstream(primary_path, tmpdir)
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Fast-forward the primary onto a commit that landed on its upstream
        land_commit_on_upstream(primary_path, origin, tmpdir)
        new_sha = _git(primary_path, "rev-parse", "HEAD")

        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "fast-forward onto upstream -> exit 0, no FAIL, no ERROR",
            exit_code == 0 and "FAIL" not in stdout and "ERROR" not in stdout,
            f"exit={exit_code}, stdout={stdout[:300]}",
        )
        h.test_result(
            "fast-forward onto upstream -> INFO head-advanced <old>..<new>",
            f"INFO {primary_path.resolve()} head-advanced {head_sha}..{new_sha}" in stdout,
            f"stdout={stdout[:300]}",
        )
        h.test_result(
            "fast-forward onto upstream -> no head-moved",
            "head-moved" not in stdout,
            f"stdout={stdout[:300]}",
        )
    print()

    print("[Test 8b] Local commit not on the upstream -> FAIL head-moved, exit 1")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        attach_upstream(primary_path, tmpdir)
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Local, unpushed commit in primary (not on the upstream)
        Path(primary_path, "newcommit.txt").write_text("new commit content")
        _git(primary_path, "add", "newcommit.txt")
        _git(primary_path, "commit", "-m", "new commit")

        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "local commit not on upstream -> exit 1",
            exit_code == 1,
            f"exit={exit_code}, stdout={stdout[:300]}",
        )
        h.test_result(
            "local commit not on upstream -> FAIL <primary> head-moved",
            f"FAIL {primary_path.resolve()} head-moved" in stdout,
            f"stdout={stdout[:300]}",
        )
        h.test_result(
            "local commit not on upstream -> no head-advanced",
            "head-advanced" not in stdout,
            f"stdout={stdout[:300]}",
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

    # Test 11: Missing baseline file -> exit 2 with baseline-unavailable on stdout
    print("[Test 11] Missing baseline file -> exit 2 with baseline-unavailable on stdout")
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
            exit_code == 2 and "baseline-unavailable" in (stdout + stderr),
            f"exit={exit_code}, stdout={stdout[:300]}, stderr={stderr[:300]}",
        )
    print()

    # Test 12: Corrupt baseline JSON -> exit 2 with baseline-unavailable on stdout
    print("[Test 12] Corrupt baseline JSON -> exit 2 with baseline-unavailable on stdout")
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
            exit_code == 2 and "baseline-unavailable" in (stdout + stderr),
            f"exit={exit_code}, stdout={stdout[:300]}, stderr={stderr[:300]}",
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

    # Test 16: snapshot with git error (non-repo path) -> exit non-zero
    print("[Test 16] snapshot with git error (non-repo path) -> exit non-zero")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        non_repo = Path(tmpdir) / "not_a_repo"

        non_repo.mkdir()

        init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))

        exit_code, stdout, stderr = run_drift_script(
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

    # Snapshot branch recording (plan Tests statements 1-3)
    print("[Test 17a] Snapshot records branch field: main branch -> 'refs/heads/main'")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"

        head_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))

        exit_code, stdout, stderr = run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )
        h.test_result(
            "snapshot exits 0",
            exit_code == 0,
            f"exit={exit_code}, stderr={stderr}",
        )

        import json
        baseline_data = json.loads(baseline_path.read_text())
        checkouts = baseline_data.get("checkouts", {})
        h.test_result(
            "baseline has checkouts dict",
            len(checkouts) > 0,
            f"checkouts={checkouts}",
        )

        primary_path_resolved = str(primary_path.resolve())
        primary_entry = checkouts.get(primary_path_resolved)
        h.test_result(
            "primary checkout entry found",
            primary_entry is not None,
            f"entries={list(checkouts.keys())}",
        )

        if primary_entry:
            h.test_result(
                "primary entry has 'branch' key",
                "branch" in primary_entry,
                f"entry={primary_entry}",
            )
            h.test_result(
                "branch value is refs/heads/main",
                primary_entry.get("branch") == "refs/heads/main",
                f"branch={primary_entry.get('branch')}",
            )
            h.test_result(
                "version is 1",
                baseline_data.get("version") == 1,
                f"version={baseline_data.get('version')}",
            )
    print()

    print("[Test 17b] Snapshot records detached HEAD as branch null")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"

        init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))

        # Detach HEAD in primary
        _git(primary_path, "checkout", "--detach")

        exit_code, stdout, stderr = run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )
        h.test_result(
            "snapshot exits 0 with detached HEAD",
            exit_code == 0,
            f"exit={exit_code}",
        )

        import json
        baseline_data = json.loads(baseline_path.read_text())
        checkouts = baseline_data.get("checkouts", {})
        primary_path_resolved = str(primary_path.resolve())
        primary_entry = checkouts.get(primary_path_resolved)
        h.test_result(
            "detached HEAD entry has branch null",
            primary_entry and primary_entry.get("branch") is None,
            f"entry={primary_entry}",
        )
    print()

    print("[Test 17c] Skipped entry (orchestrator == primary) has no branch key")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        baseline_path = Path(tmpdir) / "baseline.json"

        init_git_repo(str(primary_path))

        exit_code, stdout, stderr = run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(primary_path),
            "--out", str(baseline_path),
        )
        h.test_result(
            "snapshot exits 0 when skipped",
            exit_code == 0 and "SKIP" in stdout,
            f"exit={exit_code}, stdout={stdout}",
        )

        import json
        baseline_data = json.loads(baseline_path.read_text())
        checkouts = baseline_data.get("checkouts", {})
        primary_path_resolved = str(primary_path.resolve())
        skipped_entry = checkouts.get(primary_path_resolved)
        h.test_result(
            "skipped entry has no branch key",
            skipped_entry and "branch" not in skipped_entry,
            f"entry={skipped_entry}",
        )
    print()

    # Spec statement 5: HEAD unchanged
    print("[Test 18] HEAD unchanged: no head-advanced and no head-moved in stdout")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # No changes - HEAD stays the same
        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "HEAD unchanged: exit 0",
            exit_code == 0,
            f"exit={exit_code}",
        )
        h.test_result(
            "HEAD unchanged: no head-advanced in stdout",
            "head-advanced" not in stdout,
            f"stdout={stdout}",
        )
        h.test_result(
            "HEAD unchanged: no head-moved in stdout",
            "head-moved" not in stdout,
            f"stdout={stdout}",
        )
    print()

    # Spec statement 7: No upstream
    print("[Test 19] No upstream configured: HEAD advances -> FAIL head-moved, exit 1")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Commit in primary (no upstream)
        Path(primary_path, "commit.txt").write_text("content")
        _git(primary_path, "add", "commit.txt")
        _git(primary_path, "commit", "-m", "local commit")

        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "no upstream: exit 1",
            exit_code == 1,
            f"exit={exit_code}",
        )
        h.test_result(
            "no upstream: FAIL head-moved",
            f"FAIL {primary_path.resolve()} head-moved" in stdout,
            f"stdout={stdout}",
        )
    print()

    # Spec statement 8: reset --hard
    print("[Test 20] git reset --hard HEAD~1: baseline not ancestor -> FAIL head-moved")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        origin = attach_upstream(primary_path, tmpdir)
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        # Commit something to push to upstream
        Path(primary_path, "extra.txt").write_text("extra")
        _git(primary_path, "add", "extra.txt")
        _git(primary_path, "commit", "-m", "extra")
        _git(primary_path, "push", "origin", "main")

        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Reset hard
        _git(primary_path, "reset", "--hard", "HEAD~1")

        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "reset --hard: exit 1",
            exit_code == 1,
            f"exit={exit_code}",
        )
        h.test_result(
            "reset --hard: FAIL head-moved",
            f"FAIL {primary_path.resolve()} head-moved" in stdout,
            f"stdout={stdout}",
        )
    print()

    # Spec statement 9: amend
    print("[Test 21] git commit --amend: HEAD changed -> FAIL head-moved")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        attach_upstream(primary_path, tmpdir)
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Amend HEAD commit
        Path(primary_path, "new.txt").write_text("new")
        _git(primary_path, "add", "new.txt")
        _git(primary_path, "commit", "--amend", "-m", "amended")

        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "amend: exit 1",
            exit_code == 1,
            f"exit={exit_code}",
        )
        h.test_result(
            "amend: FAIL head-moved",
            f"FAIL {primary_path.resolve()} head-moved" in stdout,
            f"stdout={stdout}",
        )
    print()

    # Spec statement 10: switch to descendant branch
    print("[Test 22] Switch to branch descended from baseline -> FAIL head-moved")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        attach_upstream(primary_path, tmpdir)
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Create and switch to new branch with descendant commit
        _git(primary_path, "checkout", "-b", "other")
        Path(primary_path, "other.txt").write_text("other")
        _git(primary_path, "add", "other.txt")
        _git(primary_path, "commit", "-m", "other commit")

        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "switch to descendant: exit 1",
            exit_code == 1,
            f"exit={exit_code}",
        )
        h.test_result(
            "switch to descendant: FAIL head-moved",
            f"FAIL {primary_path.resolve()} head-moved" in stdout,
            f"stdout={stdout}",
        )
        h.test_result(
            "switch to descendant: no head-advanced",
            "head-advanced" not in stdout,
            f"stdout={stdout}",
        )
    print()

    # Spec statement 11: switch at same SHA
    print("[Test 23] Switch to branch at same SHA -> FAIL head-moved")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        attach_upstream(primary_path, tmpdir)
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Create new branch at same SHA and switch
        _git(primary_path, "checkout", "-b", "same_sha")

        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "switch at same SHA: exit 1",
            exit_code == 1,
            f"exit={exit_code}",
        )
        h.test_result(
            "switch at same SHA: FAIL head-moved",
            f"FAIL {primary_path.resolve()} head-moved" in stdout,
            f"stdout={stdout}",
        )
    print()

    # Spec statement 12: unrelated history
    print("[Test 24] Switch to orphan branch -> FAIL head-moved")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        attach_upstream(primary_path, tmpdir)
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Create orphan branch
        _git(primary_path, "checkout", "--orphan", "unrelated")
        _git(primary_path, "rm", "-rf", ".")
        Path(primary_path, "orphan.txt").write_text("orphan")
        _git(primary_path, "add", "orphan.txt")
        _git(primary_path, "commit", "-m", "orphan commit")

        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "orphan branch: exit 1",
            exit_code == 1,
            f"exit={exit_code}",
        )
        h.test_result(
            "orphan branch: FAIL head-moved",
            f"FAIL {primary_path.resolve()} head-moved" in stdout,
            f"stdout={stdout}",
        )
    print()

    # Spec statement 13: detached HEAD cases
    print("[Test 25] Detached at baseline and compare (same SHA) -> exit 0, no head-level line")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        attach_upstream(primary_path, tmpdir)
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        # Detach HEAD in primary before snapshot
        _git(primary_path, "checkout", "--detach")

        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Still detached at same SHA
        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "detached same SHA: exit 0",
            exit_code == 0,
            f"exit={exit_code}",
        )
        h.test_result(
            "detached same SHA: no head-level line",
            "head-advanced" not in stdout and "head-moved" not in stdout,
            f"stdout={stdout}",
        )
    print()

    print("[Test 25b] Detached at baseline, then detached at descendant -> FAIL head-moved")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        attach_upstream(primary_path, tmpdir)
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        # Detach HEAD before snapshot
        _git(primary_path, "checkout", "--detach")

        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Commit in detached state
        Path(primary_path, "detached.txt").write_text("detached")
        _git(primary_path, "add", "detached.txt")
        _git(primary_path, "commit", "-m", "detached commit")

        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "detached descendant: exit 1",
            exit_code == 1,
            f"exit={exit_code}",
        )
        h.test_result(
            "detached descendant: FAIL head-moved",
            f"FAIL {primary_path.resolve()} head-moved" in stdout,
            f"stdout={stdout}",
        )
    print()

    # Spec statement 14: legacy baseline
    print("[Test 26] Legacy baseline (no branch key): fast-forward -> FAIL head-moved")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        origin = attach_upstream(primary_path, tmpdir)
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Remove the branch key to simulate legacy baseline
        import json
        baseline_data = json.loads(baseline_path.read_text())
        checkouts = baseline_data.get("checkouts", {})
        primary_path_resolved = str(primary_path.resolve())
        if primary_path_resolved in checkouts and "branch" in checkouts[primary_path_resolved]:
            del checkouts[primary_path_resolved]["branch"]
        baseline_path.write_text(json.dumps(baseline_data))

        # Fast-forward to upstream
        land_commit_on_upstream(primary_path, origin, tmpdir)

        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "legacy baseline: exit 1",
            exit_code == 1,
            f"exit={exit_code}",
        )
        h.test_result(
            "legacy baseline: FAIL head-moved",
            f"FAIL {primary_path.resolve()} head-moved" in stdout,
            f"stdout={stdout}",
        )
        h.test_result(
            "legacy baseline: no head-advanced",
            "head-advanced" not in stdout,
            f"stdout={stdout}",
        )
    print()

    # Spec statement 15: inconclusive probe
    print("[Test 27] Inconclusive probe (invalid SHA): exit 2, ERROR probe-failed head-probe")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        attach_upstream(primary_path, tmpdir)
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Replace head with invalid SHA
        import json
        baseline_data = json.loads(baseline_path.read_text())
        checkouts = baseline_data.get("checkouts", {})
        primary_path_resolved = str(primary_path.resolve())
        if primary_path_resolved in checkouts:
            checkouts[primary_path_resolved]["head"] = "1111111111111111111111111111111111111111"
        baseline_path.write_text(json.dumps(baseline_data))

        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "inconclusive probe: exit 2",
            exit_code == 2,
            f"exit={exit_code}",
        )
        h.test_result(
            "inconclusive probe: ERROR probe-failed head-probe",
            "ERROR probe-failed head-probe" in stdout,
            f"stdout={stdout}",
        )
        h.test_result(
            "inconclusive probe: no head-advanced",
            "head-advanced" not in stdout,
            f"stdout={stdout}",
        )
        h.test_result(
            "inconclusive probe: no FAIL head-moved",
            f"FAIL {primary_path.resolve()} head-moved" not in stdout,
            f"stdout={stdout}",
        )
    print()

    # Spec statement 16: fast-forward with planned file drift
    print("[Test 28] Fast-forward + planned file drift: INFO head-advanced + FAIL ticket-...")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        origin = attach_upstream(primary_path, tmpdir)
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("driftfile.txt\n")

        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Fast-forward to upstream
        land_commit_on_upstream(primary_path, origin, tmpdir)
        new_sha = _git(primary_path, "rev-parse", "HEAD")

        # Add drift file
        Path(primary_path, "driftfile.txt").write_text("drift")

        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )
        h.test_result(
            "head-advanced + drift: exit 1",
            exit_code == 1,
            f"exit={exit_code}",
        )
        h.test_result(
            "head-advanced + drift: has INFO head-advanced",
            f"INFO {primary_path.resolve()} head-advanced {head_sha}..{new_sha}" in stdout,
            f"stdout={stdout}",
        )
        h.test_result(
            "head-advanced + drift: has FAIL drift file",
            "FAIL driftfile.txt ticket-" in stdout,
            f"stdout={stdout}",
        )
        h.test_result(
            "head-advanced + drift: primary not in ERROR paths",
            f"ERROR primary-checkout-drift" in stdout and str(primary_path.resolve()) not in stdout.split("ERROR primary-checkout-drift")[1].split("\n")[0],
            f"stdout={stdout}",
        )
    print()

    # Spec statement 17: read-only
    print("[Test 29] Read-only: refs, HEAD, status unchanged by compare during head-advanced")
    with tempfile.TemporaryDirectory() as tmpdir:
        primary_path = Path(tmpdir) / "primary"
        ticket_path = Path(tmpdir) / "ticket"
        baseline_path = Path(tmpdir) / "baseline.json"
        planned_path = Path(tmpdir) / "planned.txt"

        head_sha = init_git_repo(str(primary_path))
        origin = attach_upstream(primary_path, tmpdir)
        create_worktree(str(primary_path), str(ticket_path))
        planned_path.write_text("")

        run_drift_script(
            "snapshot",
            "--checkout", str(primary_path),
            "--orchestrator-cwd", str(ticket_path),
            "--out", str(baseline_path),
        )

        # Fast-forward
        land_commit_on_upstream(primary_path, origin, tmpdir)

        # Get state before compare (after fast-forward but before compare)
        refs_before = _git(primary_path, "for-each-ref")
        head_before = _git(primary_path, "rev-parse", "HEAD")
        status_before = get_git_status_output(str(primary_path))

        # Run compare (should not modify primary)
        exit_code, stdout, stderr = run_drift_script(
            "compare",
            "--baseline", str(baseline_path),
            "--ticket-wt", str(ticket_path),
            "--start-sha", head_sha,
            "--planned", str(planned_path),
        )

        # Get state after compare
        refs_after = _git(primary_path, "for-each-ref")
        head_after = _git(primary_path, "rev-parse", "HEAD")
        status_after = get_git_status_output(str(primary_path))

        h.test_result(
            "read-only: refs unchanged by compare",
            refs_before == refs_after,
            f"before={refs_before[:100]}, after={refs_after[:100]}",
        )
        h.test_result(
            "read-only: HEAD unchanged by compare",
            head_before == head_after,
            f"before={head_before}, after={head_after}",
        )
        h.test_result(
            "read-only: status unchanged by compare",
            status_before == status_after,
            f"before length={len(status_before)}, after length={len(status_after)}",
        )
    print()

    h.summarize_and_exit()


if __name__ == "__main__":
    main()
