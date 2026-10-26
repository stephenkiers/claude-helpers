#!/usr/bin/env python3
"""
Check-gate fixtures for scripts/flow-verify-receipt.py (#338).

An OK implement/fix receipt must carry a full-check outcome that names the configured check
command, passed, and was run against the final HEAD. A receipt that claims a pass without those
CHECK lines is rejected; so is a substituted command, a failing exit, a stale or missing
CHECK_SHA, and an opt-out the resolver does not confirm.

Each rule is exercised with a passing fixture and a failing fixture so that the rule's
positive and negative behaviour are both pinned.

Run with: python3 tests/test_flow_verify_receipt_check_gate.py
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
SINCE = "2026-10-13T00:00:00Z"
GIT_ID = ["-c", "user.email=test@example.com", "-c", "user.name=Test"]


def git(repo: str, *args: str) -> str:
    result = subprocess.run(["git", *GIT_ID, *args], cwd=repo, capture_output=True, text=True)
    return result.stdout.strip()


def make_repo(root: str) -> tuple:
    """A git repo with an initial commit and one feature commit. Returns (repo, base_sha, head_sha)."""
    repo = f"{root}/repo"
    Path(repo).mkdir(parents=True)
    git(repo, "init", "-q")
    Path(f"{repo}/initial.txt").write_text("initial\n")
    git(repo, "add", "initial.txt")
    git(repo, "commit", "-q", "-m", "initial")
    base_sha = git(repo, "rev-parse", "HEAD")
    Path(f"{repo}/feature.txt").write_text("feature\n")
    git(repo, "add", "feature.txt")
    git(repo, "commit", "-q", "-m", "add feature")
    head_sha = git(repo, "rev-parse", "HEAD")
    return repo, base_sha, head_sha


def write_cache(repo: str, command: str = "just check") -> None:
    """Configure commands.check in the repo's repo-cache so the resolver finds a command."""
    cache_dir = Path(repo) / ".claude"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "repo-cache.json").write_text(json.dumps({"commands": {"check": command}}))


def write_opt_out(repo: str, reason: str = "docs-only repo") -> None:
    """Opt the repo out of the implement/fix check gate via project.yaml checkOptOut."""
    cache_dir = Path(repo) / ".claude"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "project.yaml").write_text(f'checkOptOut: "{reason}"\n')


def passing_lines(head_sha: str, command: str = "just check", source: str = "repo-cache") -> str:
    return f"CHECK: {command}\nCHECK_EXIT: 0\nCHECK_SOURCE: {source}\nCHECK_SHA: {head_sha}\n"


def receipt_text(check_block: str) -> str:
    return "ROUND 1\n" + check_block + "OUTSTANDING: none\nDecision: OK\n<!-- step-end -->"


def run_verify(receipt_path: str, repo: str, base_sha: str, home: str, main_worktree=None) -> tuple:
    cmd = [
        sys.executable, str(VERIFIER),
        "--step", "implement",
        "--receipt", receipt_path,
        "--since", SINCE,
        "--repo", repo,
        "--branch", "HEAD",
        "--base", base_sha,
    ]
    if main_worktree is not None:
        cmd.extend(["--main-worktree", main_worktree])
    env = os.environ.copy()
    env["HOME"] = home
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def write_receipt(home: str, text: str) -> str:
    path = f"{home}/receipt.md"
    Path(path).write_text(text)
    return path


if __name__ == "__main__":
    h = Harness("FLOW VERIFY RECEIPT CHECK GATE TEST SUITE")
    test_result = h.test_result

    print("[Section 1] Rule (a): an OK receipt must carry the full-check outcome (#293 reproduction)")

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo)
        # #293: the receipt claims a build/type-check pass in prose and carries no CHECK lines.
        receipt = write_receipt(home, "ROUND 1\nBuild/type-check: pass (ruff, doc tests)\n"
                                      "OUTSTANDING: none\nDecision: OK\n<!-- step-end -->")
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "#293 repro: prose 'pass' with no CHECK lines is rejected (exit 1)",
            exit_code == 1,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )
        test_result(
            "#293 repro: rejection reason is 'check outcome missing from receipt'",
            "check outcome missing from receipt" in stdout,
            f"stdout={stdout[:300]!r}",
        )

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo)
        receipt = write_receipt(home, receipt_text(passing_lines(head_sha)))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (a) positive: an OK receipt with a complete passing CHECK block passes",
            exit_code == 0 and "ARTIFACTS: ok" in stdout,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    print()
    print("[Section 2] Rule (b): the CHECK command must be the one the repo configures")

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo, "just check")
        receipt = write_receipt(home, receipt_text(passing_lines(head_sha, command="ruff check .", source="repo-cache")))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (b) negative: a substituted command (ruff for 'just check') is rejected",
            exit_code == 1 and "check substituted" in stdout,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo, "just check")
        receipt = write_receipt(home, receipt_text(passing_lines(head_sha, command="just check", source="repo-cache")))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (b) positive: the configured command ('just check') is accepted",
            exit_code == 0,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    print()
    print("[Section 3] Rule (c): the CHECK run must have passed")

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo)
        failing = passing_lines(head_sha).replace("CHECK_EXIT: 0", "CHECK_EXIT: 1")
        receipt = write_receipt(home, receipt_text(failing))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (c) negative: CHECK_EXIT: 1 is rejected with 'check did not pass'",
            exit_code == 1 and "check did not pass" in stdout,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo)
        receipt = write_receipt(home, receipt_text(passing_lines(head_sha)))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (c) positive: CHECK_EXIT: 0 passes",
            exit_code == 0,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    print()
    print("[Section 4] Rule (d): CHECK_SHA must name the final HEAD")

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo)
        # The check was run on the parent commit, not the final HEAD.
        receipt = write_receipt(home, receipt_text(passing_lines(base_sha)))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (d) negative: CHECK_SHA naming the parent (not HEAD) is rejected",
            exit_code == 1,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo)
        no_sha = "CHECK: just check\nCHECK_EXIT: 0\nCHECK_SOURCE: repo-cache\n"
        receipt = write_receipt(home, receipt_text(no_sha))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (d) negative: a CHECK block with no CHECK_SHA is rejected",
            exit_code == 1,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo)
        receipt = write_receipt(home, receipt_text(passing_lines(head_sha)))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (d) positive: CHECK_SHA equal to the final HEAD passes",
            exit_code == 0,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    print()
    print("[Section 5] Rule (e): an opt-out is accepted only when the resolver confirms it")

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_opt_out(repo, "docs-only repo")
        opted_out = "CHECK: opted-out\nCHECK_SOURCE: opt-out\nCHECK_SHA: " + head_sha + "\n"
        receipt = write_receipt(home, receipt_text(opted_out))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (e) positive: CHECK: opted-out is accepted when project.yaml checkOptOut is set",
            exit_code == 0,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        # No checkOptOut and no command configured: an opt-out claim is not confirmed by the resolver.
        opted_out = "CHECK: opted-out\nCHECK_SOURCE: opt-out\nCHECK_SHA: " + head_sha + "\n"
        receipt = write_receipt(home, receipt_text(opted_out))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (e) negative: CHECK: opted-out with no resolver opt-out is rejected",
            exit_code == 1,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    print()
    print("[Section 6] Rule (f): an OK receipt with no command and no opt-out is rejected")

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        # Nothing configured anywhere: the resolver has no command and no opt-out.
        receipt = write_receipt(home, receipt_text(passing_lines(head_sha)))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (f) negative: a passing CHECK block with no configured command is rejected",
            exit_code == 1,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        write_cache(repo)
        receipt = write_receipt(home, receipt_text(passing_lines(head_sha)))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home)
        test_result(
            "rule (f) positive: a configured command with a passing CHECK block passes",
            exit_code == 0,
            f"rc={exit_code} stdout={stdout[:300]!r}",
        )

    print()
    print("[Section 7] --main-worktree: the command may come from the main worktree's cache")

    with tempfile.TemporaryDirectory() as home:
        repo, base_sha, head_sha = make_repo(home)
        main = f"{home}/main"
        Path(main).mkdir(parents=True)
        write_cache(main, "just check")
        receipt = write_receipt(home, receipt_text(passing_lines(head_sha)))
        exit_code, stdout, stderr = run_verify(receipt, repo, base_sha, home, main_worktree=main)
        test_result(
            "--main-worktree positive: command from the main cache, worktree has none, passes",
            exit_code == 0,
            f"rc={exit_code} stdout={stdout[:300]!r} stderr={stderr[:200]!r}",
        )

    print()
    h.summarize_and_exit()
